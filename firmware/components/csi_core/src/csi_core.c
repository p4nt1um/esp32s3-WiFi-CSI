#include "csi_core.h"

#include <math.h>
#include <stdlib.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

/* 参数（与 dsp.py 默认值一致；参数域见 csi_core.h 注释） */
#define HAMPEL_HALF   7
#define HAMPEL_NSIG   3.0
#define HAMPEL_EPS    1e-6
#define MAD_SCALE     1.4826
#define FS_OUT        10.0
#define DC_WIN_S      10.0
#define TOPK          CSI_CORE_TOPK
#define BAND_LO       0.1
#define BAND_HI       0.6
#define WIN_S         30.0
#define STEP_S        1.0
#define NFFT          512
#define SHORT_WIN_S   7.5
#define MOTION_K      8.0
#define LOW_K         0.3
#define MIN_PEAK_R    3.5   /* 标定史 5.0→3.5(桌面+床,验收采用)→3.0(覆盖模式,挂起率破线已回退) */
#define N_SOS         4

/* 带通 0.1–0.6Hz Butterworth 4 阶 @10Hz SOS（行 [b0,b1,b2,a1,a2]，a0=1）
 * 由 analysis/tools/export_sos.py 生成；改参数后须重新生成并复测对拍 */
static const double k_sos[N_SOS][5] = {
    {0.00041659920440659915, 0.0008331984088131983, 0.00041659920440659915,
     -1.569941456557534, 0.63532986146777271},
    {1, 2, 1, -1.6964162322247374, 0.81749411483217527},
    {1, -2, 1, -1.8682609143877715, 0.87485505875388991},
    {1, -2, 1, -1.9604388489082369, 0.96453261926805467},
};

/* ---------------- 小工具 ---------------- */

static int refl_idx(int i, int n)   /* scipy.ndimage 'reflect'：边缘值重复（= numpy.pad symmetric） */
{
    if (i < 0) return -i - 1;
    if (i >= n) return 2 * n - 1 - i;
    return i;
}

static void sort_floats(double *a, int n)   /* 插入排序（n ≤ 15） */
{
    for (int i = 1; i < n; i++) {
        double v = a[i];
        int j = i - 1;
        while (j >= 0 && a[j] > v) { a[j + 1] = a[j]; j--; }
        a[j + 1] = v;
    }
}

static double parabolic(const double *y, int i, int n)   /* 与 dsp._parabolic 同式 */
{
    if (i > 0 && i < n - 1) {
        double den = (double)y[i - 1] - 2.0 * y[i] + (double)y[i + 1];
        if (fabs(den) > 1e-12)
            return i + 0.5 * ((double)y[i - 1] - (double)y[i + 1]) / den;
    }
    return (double)i;
}

/* ---------------- radix-2 FFT（float32，原位） ---------------- */

static void fft_pow2(double *re, double *im, int n)
{
    for (int i = 1, j = 0; i < n; i++) {          /* 位反转置换 */
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            double t = re[i]; re[i] = re[j]; re[j] = t;
            t = im[i]; im[i] = im[j]; im[j] = t;
        }
    }
    for (int len = 2; len <= n; len <<= 1) {
        double ang = -2.0 * M_PI / len;
        for (int i = 0; i < n; i += len) {
            for (int k = 0; k < len / 2; k++) {
                double wr = (double)cos(ang * k), wi = (double)sin(ang * k);
                int a = i + k, b = i + k + len / 2;
                double xr = re[b] * wr - im[b] * wi;
                double xi = re[b] * wi + im[b] * wr;
                re[b] = re[a] - xr; im[b] = im[a] - xi;
                re[a] += xr;        im[a] += xi;
            }
        }
    }
}

/* ---------------- 管线步骤 ---------------- */

static void step_amplitude(const int16_t *iq, int n, int s, double *amp)
{
    for (int i = 0; i < n; i++)
        for (int k = 0; k < s; k++) {
            double fi = (double)iq[(size_t)i * s * 2 + k * 2];
            double fq = (double)iq[(size_t)i * s * 2 + k * 2 + 1];
            amp[(size_t)i * s + k] = sqrt(fi * fi + fq * fq);
        }
}

static void step_hampel(double *x, int n, int s)   /* ① 100Hz 域（与 scipy 两遍语义一致） */
{
    const int w = 2 * HAMPEL_HALF + 1;
    double buf[16];
    double *med = malloc(sizeof(double) * (size_t)n * s);     /* 逐点窗口中值 */
    double *devimg = malloc(sizeof(double) * (size_t)n * s);  /* 偏差图 |x - med| */
    if (!med || !devimg) { free(med); free(devimg); return; }

    for (int k = 0; k < s; k++) {                             /* 第一遍：中值图 */
        for (int i = 0; i < n; i++) {
            for (int j = 0; j < w; j++)
                buf[j] = x[(size_t)refl_idx(i + j - HAMPEL_HALF, n) * s + k];
            sort_floats(buf, w);
            med[(size_t)i * s + k] = buf[w / 2];
            devimg[(size_t)i * s + k] = fabs(x[(size_t)i * s + k] - buf[w / 2]);
        }
    }
    for (int k = 0; k < s; k++) {                             /* 第二遍：MAD = 偏差图的窗口中值，替换 */
        for (int i = 0; i < n; i++) {
            for (int j = 0; j < w; j++)
                buf[j] = devimg[(size_t)refl_idx(i + j - HAMPEL_HALF, n) * s + k];
            sort_floats(buf, w);
            double mad = buf[w / 2];
            double m = med[(size_t)i * s + k];
            double cur = x[(size_t)i * s + k];
            x[(size_t)i * s + k] =
                fabs(cur - m) > (double)(HAMPEL_NSIG * (MAD_SCALE * mad + HAMPEL_EPS)) ? m : cur;
        }
    }
    free(med); free(devimg);
}

static int step_downsample(const double *x, int n, int s, int q, double *y)  /* ② 块平均 */
{
    int n2 = n / q;
    for (int i = 0; i < n2; i++)
        for (int k = 0; k < s; k++) {
            double acc = 0.0;
            for (int j = 0; j < q; j++) acc += x[(size_t)(i * q + j) * s + k];
            y[(size_t)i * s + k] = acc / (double)q;
        }
    return n2;
}

static void step_remove_dc(double *x, int n, int s, int win)   /* ③ 滑窗均值去DC（reflect 填充，基线全部取自原始数据） */
{
    int half = win / 2;
    double *out = malloc(sizeof(double) * (size_t)n * s);
    if (!out) return;
    for (int k = 0; k < s; k++) {
        for (int i = 0; i < n; i++) {
            double sum = 0.0;
            for (int j = i - half; j <= i + half; j++)
                sum += x[(size_t)refl_idx(j, n) * s + k];   /* 与 np.pad reflect 同映射 */
            out[(size_t)i * s + k] = x[(size_t)i * s + k] - sum / win;
        }
    }
    memcpy(x, out, sizeof(double) * (size_t)n * s);
    free(out);
}

static int band_range(int n_fft, double fs, double lo_hz, double hi_hz,
                      int *kmin, int *kmax)   /* 与 numpy 频带掩码同判据 */
{
    double denom = n_fft * (1.0 / fs);   /* = rfftfreq 的 n*d */
    *kmin = -1; *kmax = -1;
    for (int k = 0; k <= n_fft / 2; k++) {
        double f = k / denom;
        if (*kmin < 0 && f >= lo_hz) *kmin = k;
        if (f <= hi_hz) *kmax = k;
    }
    if (*kmin < 0 || *kmax < *kmin) return -1;
    return 0;
}

static int step_topk(const double *x, int n, int s, double *agg, int *idx_out)  /* ④ */
{
    int n_fft = 1;
    while (n_fft < n) n_fft <<= 1;
    double *re = malloc(sizeof(double) * (size_t)n_fft);
    double *im = malloc(sizeof(double) * (size_t)n_fft);
    double *ratio = calloc((size_t)s, sizeof(double));
    int kmin, kmax;
    if (!re || !im || !ratio || band_range(n_fft, FS_OUT, BAND_LO, BAND_HI, &kmin, &kmax) != 0) {
        free(re); free(im); free(ratio);
        return -1;
    }

    double *hannk = malloc(sizeof(double) * (size_t)n);
    if (!hannk) { free(re); free(im); free(ratio); return -1; }
    for (int i = 0; i < n; i++)
        hannk[i] = 0.5 - 0.5 * cos(2.0 * M_PI * i / (n - 1));   /* hann 抑制泄漏裙边 */

    for (int k = 0; k < s; k++) {
        double mean = 0.0;
        for (int i = 0; i < n; i++) mean += x[(size_t)i * s + k];
        mean /= n;
        for (int i = 0; i < n; i++) { re[i] = (x[(size_t)i * s + k] - mean) * hannk[i]; im[i] = 0.0; }
        for (int i = n; i < n_fft; i++) { re[i] = 0.0; im[i] = 0.0; }
        fft_pow2(re, im, n_fft);
        double total = 0.0, band = 0.0;
        for (int b = 0; b <= n_fft / 2; b++) {
            double p = (double)re[b] * re[b] + (double)im[b] * im[b];
            total += p;
            if (b >= kmin && b <= kmax) band += p;
        }
        ratio[k] = band / (total + 1e-9);
    }

    /* 排序键量化到 1e-4（floor），平手按索引升序——与 Python 端同序；
     * 量化粒度远大于 libm 末位差异（~1e-15）、远小于子载波间真实差距（~1e-3） */
    long *rq = malloc(sizeof(long) * (size_t)s);
    for (int k = 0; k < s; k++) rq[k] = (long)(ratio[k] * 10000.0);
    for (int r = 0; r < TOPK; r++) {
        int best = -1;
        for (int k = 0; k < s; k++) {
            if (rq[k] < 0) continue;            /* 已选过的标记为 -1 */
            if (best < 0 || rq[k] > rq[best]) best = k;
        }
        idx_out[r] = best;
        rq[best] = -1;
    }
    free(rq);

    for (int i = 0; i < n; i++) {
        double acc = 0.0;
        for (int r = 0; r < TOPK; r++) acc += x[(size_t)i * s + idx_out[r]];
        agg[i] = acc / (double)TOPK;
    }
    free(re); free(im); free(ratio); free(hannk);
    return 0;
}

static void sos_cascade(const double sos[][5], int n_sec, double *buf, int n)  /* DF2T 原地级联 */
{
    for (int sec = 0; sec < n_sec; sec++) {
        const double b0 = sos[sec][0], b1 = sos[sec][1], b2 = sos[sec][2];
        const double a1 = sos[sec][3], a2 = sos[sec][4];
        double z1 = 0.0, z2 = 0.0;
        for (int i = 0; i < n; i++) {
            double xi = buf[i];               /* DF2T 逐样本仅依赖当前输入 → 原地安全 */
            double y = b0 * xi + z1;
            z1 = b1 * xi - a1 * y + z2;
            z2 = b2 * xi - a2 * y;
            buf[i] = (double)y;
        }
    }
}

static void step_bandpass(const double *in, double *out, int n)   /* ⑤ 双通零相位 */
{
    double *tmp = malloc(sizeof(double) * (size_t)n);
    if (!tmp) { memcpy(out, in, sizeof(double) * (size_t)n); return; }
    memcpy(tmp, in, sizeof(double) * (size_t)n);
    sos_cascade(k_sos, N_SOS, tmp, n);                 /* 正向 */
    for (int i = 0; i < n; i++) out[n - 1 - i] = tmp[i];   /* 反转 */
    sos_cascade(k_sos, N_SOS, out, n);                 /* 反向 */
    for (int i = 0; i < n / 2; i++) {                  /* 再反转 */
        double t = out[i]; out[i] = out[n - 1 - i]; out[n - 1 - i] = t;
    }
    free(tmp);
}

/* ---------------- 主入口 ---------------- */

int csi_core_run(const csi_core_input *in, double fs_in, csi_core_result *out)
{
    if (!in || !out || !in->iq || in->n_sub <= 0 || in->n_sub > CSI_CORE_MAX_SUB
        || in->n_samples < 64)
        return -1;

    int q = (int)(fs_in / FS_OUT + 0.5);
    if (q < 1) q = 1;
    double fs = fs_in / q;

    int n = in->n_samples, s = in->n_sub;
    double *amp = malloc(sizeof(double) * (size_t)n * s);
    if (!amp) return -2;

    step_amplitude(in->iq, n, s, amp);
    step_hampel(amp, n, s);

    int n2 = n / q;
    double *amp2 = malloc(sizeof(double) * (size_t)n2 * s);
    if (!amp2) { free(amp); return -2; }
    step_downsample(amp, n, s, q, amp2);
    free(amp);
    if (n2 < (int)(WIN_S * fs)) { free(amp2); return -3; }
    step_remove_dc(amp2, n2, s, (int)(DC_WIN_S * fs) | 1);

    double *agg = malloc(sizeof(double) * (size_t)n2);
    if (!agg) { free(amp2); return -2; }
    if (step_topk(amp2, n2, s, agg, out->top_idx) != 0) { free(amp2); free(agg); return -4; }
    out->n_top = TOPK;

    double *aggf = malloc(sizeof(double) * (size_t)n2);
    if (!aggf) { free(amp2); free(agg); return -2; }
    step_bandpass(agg, aggf, n2);

    /* ⑥⑦ 滑窗估计 + 门控 */
    int win = (int)(WIN_S * fs), step = (int)(STEP_S * fs);
    int kmin, kmax;
    band_range(NFFT, fs, BAND_LO, BAND_HI, &kmin, &kmax);
    int n_win = (n2 - win) / step + 1;
    int short_win = (int)(SHORT_WIN_S * fs);
    int half_sw = short_win / 2;
    double denom = NFFT * (1.0 / fs);

    double *seg = malloc(sizeof(double) * (size_t)win);
    double *re = malloc(sizeof(double) * NFFT);
    double *im = malloc(sizeof(double) * NFFT);
    double *hann = malloc(sizeof(double) * (size_t)win);
    double *band_mag = malloc(sizeof(double) * (size_t)(kmax - kmin + 1));
    double *acslice = malloc(sizeof(double) * (size_t)win);
    double *acs = malloc(sizeof(double) * (size_t)win);   /* 自相关切片：独立缓冲，严禁复用段缓冲（会自污染） */
    double *pw = malloc(sizeof(double) * (size_t)n_win);
    if (!seg || !re || !im || !hann || !band_mag || !acslice || !acs || !pw) {
        free(seg); free(re); free(im); free(hann); free(band_mag); free(acslice); free(acs); free(pw);
        free(amp2); free(agg); free(aggf);
        return -2;
    }
    for (int i = 0; i < win; i++)
        hann[i] = (double)(0.5 - 0.5 * cos(2.0 * M_PI * i / (win - 1)));

    for (int w = 0; w < n_win; w++) {
        int st = w * step;
        csi_core_window *r = &out->win[w];

        /* FFT 路（hann 加窗，512 零填充） */
        double mean_raw = 0.0;
        for (int i = 0; i < win; i++) mean_raw += aggf[st + i];
        mean_raw /= win;
        for (int i = 0; i < win; i++) { re[i] = aggf[st + i] * hann[i]; im[i] = 0.0; }
        for (int i = win; i < NFFT; i++) { re[i] = 0.0; im[i] = 0.0; }
        fft_pow2(re, im, NFFT);
        int nb = kmax - kmin + 1;
        int ipk = 0;
        double band_sum = 0.0;
        for (int b = 0; b < nb; b++) {
            band_mag[b] = sqrt(re[kmin + b] * re[kmin + b] + im[kmin + b] * im[kmin + b]);
            band_sum += band_mag[b];
        }
        {   /* 相对量化（基准=带内最大，粒度 1e-10 相对）：吸收 FFT 累加次序差，并列取低 bin（与 Python 同规） */
            double bmax = 0.0;
            for (int b = 0; b < nb; b++) if (band_mag[b] > bmax) bmax = band_mag[b];
            bmax += 1e-300;
            int64_t qpk = -1;
            for (int b = 0; b < nb; b++) {
                int64_t q = (int64_t)(band_mag[b] / bmax * 1e10 + 0.5);
                if (q > qpk) { qpk = q; ipk = b; }
            }
        }
        double frac = parabolic(band_mag, ipk, nb);
        double f_fft = kmin / denom + frac * (1.0 / denom);

        /* 自相关路（去均值不加窗，滞后 16..100） */
        double c0 = 0.0;
        for (int i = 0; i < win; i++) {
            acslice[i] = (double)(aggf[st + i] - mean_raw);
            c0 += (double)acslice[i] * acslice[i];
        }
        int lo_lag = (int)(fs / BAND_HI), hi_lag = (int)(fs / BAND_LO);
        if (lo_lag < 2) lo_lag = 2;
        if (hi_lag > win - 1) hi_lag = win - 1;
        int nlag = hi_lag - lo_lag + 1;
        int iac = 0;
        for (int li = 0; li < nlag; li++) {
            int lag = lo_lag + li;
            double acc = 0.0;
            for (int i = 0; i + lag < win; i++) acc += (double)acslice[i] * acslice[i + lag];
            acs[li] = (double)(acc / (c0 + 1e-12));
        }
        {   /* 相对量化并列取低 lag（与 Python 同规） */
            double amax = 0.0;
            for (int li = 0; li < nlag; li++) if (acs[li] > amax) amax = acs[li];
            amax += 1e-300;
            int64_t qac = -1;
            for (int li = 0; li < nlag; li++) {
                int64_t q = (int64_t)(acs[li] / amax * 1e10 + 0.5);
                if (q > qac) { qac = q; iac = li; }
            }
        }
        double lag = lo_lag + parabolic(acs, iac, nlag);

        double f_ac = fs / lag;
        r->bpm_fft = f_fft * 60.0;
        r->bpm_ac = f_ac * 60.0;
        r->agree = fabs(f_fft - f_ac) <= (0.06 * f_fft > 0.02 ? 0.06 * f_fft : 0.02);
        r->peak_ratio = band_mag[ipk] / (band_sum / nb + 1e-12);
        r->t = (st + win / 2) / fs;

        /* ⑦ 门控量：7.5s 短窗功率（中心采样，边缘补零） */
        int ci = st + win / 2;
        double p = 0.0;
        for (int i = ci - half_sw; i <= ci + half_sw; i++)
            if (i >= 0 && i < n2) p += (double)aggf[i] * aggf[i];
        pw[w] = p / short_win;
    }

    /* 基线 = 功率中位数（numpy percentile 线性插值语义） */
    double *sorted = malloc(sizeof(double) * (size_t)n_win);
    if (!sorted) { free(pw); free(seg); free(re); free(im); free(hann); free(band_mag); free(acslice); free(acs);
                   free(amp2); free(agg); free(aggf); return -2; }
    memcpy(sorted, pw, sizeof(double) * (size_t)n_win);
    for (int i = 1; i < n_win; i++) {
        double v = sorted[i];
        int j = i - 1;
        while (j >= 0 && sorted[j] > v) { sorted[j + 1] = sorted[j]; j--; }
        sorted[j + 1] = v;
    }
    double pos = (n_win - 1) * 0.5;
    int lo_i = (int)floor(pos);
    double frac2 = pos - lo_i;
    double base = sorted[lo_i];
    if (frac2 > 0 && lo_i + 1 < n_win) base += frac2 * (sorted[lo_i + 1] - sorted[lo_i]);

    for (int w = 0; w < n_win; w++) {
        csi_core_window *r = &out->win[w];
        r->power = pw[w];
        r->valid = (pw[w] < base * MOTION_K) && (pw[w] > base * LOW_K)
                   && (r->peak_ratio >= MIN_PEAK_R) && r->agree;   /* 旧快速判据，随后被 ⑧ 覆盖 */
    }

    /* ⑧ 相干轨迹确认（宽进严出，最终 valid；与 Python coherence_confirm 同构）：
       宽进 = agree & 峰值比≥2.0 & 功率非尖峰(<P75×3)；
       严出 = ±45s 邻域内候选≥5 且本窗估计与邻域中位差≤0.5bpm。
       依据：呼吸为单一生理源，真信号窗锁定同一频率轨迹；走动/干扰伪窗散乱出局。 */
    {
        double pos75 = (n_win - 1) * 0.75;                 /* numpy percentile 线性插值语义 */
        int lo75 = (int)floor(pos75);
        double frac75 = pos75 - lo75;
        double p75 = sorted[lo75];
        if (frac75 > 0 && lo75 + 1 < n_win) p75 += frac75 * (sorted[lo75 + 1] - sorted[lo75]);

        int *cand = malloc(sizeof(int) * (size_t)n_win);
        double *est = malloc(sizeof(double) * (size_t)n_win);
        double *nbbuf = malloc(sizeof(double) * (size_t)(2 * 45 + 1));
        if (cand && est && nbbuf) {
            const int half = 45, min_nb = 5;
            for (int w = 0; w < n_win; w++) {
                csi_core_window *r = &out->win[w];
                est[w] = (r->bpm_fft + r->bpm_ac) / 2.0;
                cand[w] = r->agree && r->peak_ratio >= 2.0 && r->power < p75 * 3.0;
                r->candidate = cand[w];
            }
            for (int w = 0; w < n_win; w++) {
                csi_core_window *r = &out->win[w];
                int conf = 0;
                if (cand[w]) {
                    int lo = w - half < 0 ? 0 : w - half;
                    int hi = w + half > n_win - 1 ? n_win - 1 : w + half;
                    int cnt = 0;
                    for (int j = lo; j <= hi; j++)
                        if (cand[j]) nbbuf[cnt++] = est[j];
                    if (cnt >= min_nb) {
                        sort_floats(nbbuf, cnt);            /* cnt ≤ 91 */
                        double med = (cnt % 2) ? nbbuf[cnt / 2]
                                               : (nbbuf[cnt / 2 - 1] + nbbuf[cnt / 2]) / 2.0;
                        if (fabs(est[w] - med) <= 0.5) conf = 1;
                    }
                }
                r->valid = conf;
                r->bpm = conf ? est[w] : NAN;
            }
        }
        free(cand); free(est); free(nbbuf);
    }
    out->n_win = n_win;

    free(sorted); free(pw); free(seg); free(re); free(im); free(hann);
    free(band_mag); free(acslice); free(acs);
    free(amp2); free(agg); free(aggf);
    return 0;
}

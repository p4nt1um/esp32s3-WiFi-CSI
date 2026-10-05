/* 中间量调试：打印每子载波呼吸频带能量占比（与 Python topk 逐步对拍用） */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "csi_core.c"   /* 直接包含以访问 static 步骤函数 */

#define LINE_BUF 8192
static int16_t *s_iq;
static size_t s_cap, s_cnt;
static int s_nsub;

static void push_iq(int v)
{
    if (s_cnt == s_cap) {
        s_cap = s_cap ? s_cap * 2 : 64 * 104;
        s_iq = realloc(s_iq, sizeof(int16_t) * s_cap);
    }
    s_iq[s_cnt++] = (int16_t)v;
}

int main(int argc, char **argv)
{
    if (argc < 2) return 1;
    FILE *f = fopen(argv[1], "r");
    char line[LINE_BUF];
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "CSI_DATA,", 9)) continue;
        const char *p = strstr(line, ",\"[");
        if (!p) continue;
        if (!s_nsub) {
            int n = 0;
            for (const char *r = p + 3; *r && *r != ']'; r++) if (*r == ',') n++;
            s_nsub = (n + 1) / 2;
        }
        p += 3;
        while (*p && *p != ']') {
            char *end;
            long v = strtol(p, &end, 10);
            if (end == p) break;
            push_iq((int)v);
            p = end;
            if (*p == ',') p++;
        }
    }
    fclose(f);

    int n = (int)(s_cnt / (size_t)(s_nsub * 2)), s = s_nsub, q = 10, n2 = n / q;
    double *amp = malloc(sizeof(double) * (size_t)n * s);
    step_amplitude(s_iq, n, s, amp);
    if (argc > 3) {                                   /* 幅度级导出 */
        FILE *bo = fopen(argv[3], "wb");
        fwrite(amp, sizeof(double), (size_t)n * s, bo);
        fclose(bo);
    }
    step_hampel(amp, n, s);
    if (argc > 4) {                                   /* hampel 级导出 */
        FILE *bo = fopen(argv[4], "wb");
        fwrite(amp, sizeof(double), (size_t)n * s, bo);
        fclose(bo);
    }
    double *amp2 = malloc(sizeof(double) * (size_t)n2 * s);
    step_downsample(amp, n, s, q, amp2);
    step_remove_dc(amp2, n2, s, 101);

    /* 复制 step_topk 的比值计算并打印 */
    int n_fft = 1;
    while (n_fft < n2) n_fft <<= 1;
    double *re = malloc(sizeof(double) * (size_t)n_fft);
    double *im = malloc(sizeof(double) * (size_t)n_fft);
    int kmin, kmax;
    band_range(n_fft, 10.0, 0.1, 0.6, &kmin, &kmax);
    /* 导出完整 amp2 供与 Python 逐元素比对 */
    if (argc > 2) {
        FILE *bo = fopen(argv[2], "wb");
        fwrite(amp2, sizeof(double), (size_t)n2 * s, bo);
        fclose(bo);
        printf("amp2 dumped %d x %d\n", n2, s);
    }
    printf("n2=%d n_fft=%d kmin=%d kmax=%d\n", n2, n_fft, kmin, kmax);
    printf("C sub21[:8]:");
    for (int i = 0; i < 8; i++) printf(" %.6f", amp2[(size_t)i * s + 21]);
    printf("\n");
    { double ss = 0; for (int i = 0; i < n2; i++) { double v = amp2[(size_t)i * s + 21]; ss += v * v; }
      printf("C sub21 rms: %.6f\n", sqrt(ss / n2)); }
    for (int k = 0; k < s; k++) {
        double mean = 0;
        for (int i = 0; i < n2; i++) mean += amp2[(size_t)i * s + k];
        mean /= n2;
        for (int i = 0; i < n2; i++) { re[i] = amp2[(size_t)i * s + k] - mean; im[i] = 0; }
        for (int i = n2; i < n_fft; i++) { re[i] = 0; im[i] = 0; }
        fft_pow2(re, im, n_fft);
        double total = 0, band = 0;
        for (int b = 0; b <= n_fft / 2; b++) {
            double p = re[b] * re[b] + im[b] * im[b];
            total += p;
            if (b >= kmin && b <= kmax) band += p;
        }
        printf("sub %d ratio %.9f\n", k, band / (total + 1e-9));
    }
    return 0;
}

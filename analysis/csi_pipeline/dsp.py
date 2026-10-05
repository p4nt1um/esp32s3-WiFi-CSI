"""DSP 原语：Hampel、降采样、去DC、子载波聚合、带通、呼吸率估计、体动门控。

所有参数默认值对应《项目实施方案》§6，标注了参数所属的采样率域
（Hampel 的"窗15"是 100Hz 域参数 = 0.15s，降采样后不可沿用 —— 评审 P1-1）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import median_filter
from scipy.signal import butter, sosfilt

BREATH_BAND = (0.1, 0.6)   # Hz，6–36 bpm（Serene 10–40 放宽下限）


# ---------- ⓪ 按时间戳重采样到均匀网格（真实链路丢包修复） ----------

def resample_uniform(ts_us: np.ndarray, x: np.ndarray, fs: float = 100.0) -> tuple[np.ndarray, np.ndarray]:
    """CSI 包含随机丢失时时间轴有洞，块平均等均匀采样假设失效（波形相位抖动→FFT 展宽）。
    线性插值到 fs 网格；两端截断到数据实际覆盖范围。"""
    t = ts_us.astype(np.float64) / 1e6
    grid = np.arange(t[0], t[-1] + 0.5 / fs, 1.0 / fs)   # 含端点：无损包时与原始行数一致（对拍契约）
    out = np.empty((len(grid), x.shape[1]), dtype=np.float64)
    for k in range(x.shape[1]):
        out[:, k] = np.interp(grid, t, x[:, k])
    return (grid * 1e6).astype(np.int64), out


# ---------- ① Hampel 去野点（100Hz 域） ----------

def hampel(x: np.ndarray, window: int = 15, n_sigmas: float = 3.0) -> np.ndarray:
    """x: (N, S)。窗口为奇数半径内的滑动中值 ±n_sigmas·MAD 之外的点替换为中值。"""
    half = window // 2
    med = median_filter(x, size=(2 * half + 1, 1), mode="reflect")
    mad = median_filter(np.abs(x - med), size=(2 * half + 1, 1), mode="reflect")
    outlier = np.abs(x - med) > n_sigmas * (1.4826 * mad + 1e-6)
    return np.where(outlier, med, x)


# ---------- ② 抗混叠降采样 100→10Hz ----------

def downsample(x: np.ndarray, fs_in: float, fs_out: float = 10.0) -> tuple[np.ndarray, float]:
    """块平均抗混叠 + 抽取（确定性实现，与 C 端 csi_core 逐位一致，用于对拍；
    替代此前的 scipy.decimate IIR——呼吸频段远离混叠区，盒式滤波足够）。"""
    q = int(round(fs_in / fs_out))
    if q <= 1:
        return x, fs_in
    n = (len(x) // q) * q
    return x[:n].reshape(-1, q, x.shape[1]).mean(axis=1), fs_in / q


# ---------- ③ 去DC（10Hz 域） ----------

def remove_dc(x: np.ndarray, fs: float = 10.0, win_s: float = 10.0) -> np.ndarray:
    """滑窗均值去DC（reflect 填充——补零会在两端产生瞬态，泄漏进呼吸带压过弱信号）。"""
    n = max(3, int(win_s * fs) | 1)
    k = np.ones(n) / n
    h = n // 2
    xp = np.pad(x, ((h, h), (0, 0)), mode="symmetric")   # 注意：np.pad 的 reflect 是"不重复边缘"，symmetric 才等价 scipy.ndimage reflect / C 端 refl_idx
    base = np.apply_along_axis(lambda c: np.convolve(c, k, mode="valid"), 0, xp)
    return x - base


# ---------- ④ 子载波聚合 ----------

def _band_mask(n: int, fs: float, band=BREATH_BAND) -> np.ndarray:
    f = np.fft.rfftfreq(n, 1.0 / fs)
    return (f >= band[0]) & (f <= band[1])


def topk_subcarriers(x: np.ndarray, fs: float, k: int = 8,
                     band=BREATH_BAND) -> tuple[np.ndarray, np.ndarray]:
    """按呼吸频段能量占比选 top-k 子载波并求均值（WiRM 思路）。
    返回 (聚合信号, 选中索引)。"""
    n_fft = 1 << int(np.ceil(np.log2(x.shape[0])))     # pow2 零填充，与 C 端 radix-2 一致
    xc = x - x.mean(axis=0)
    xc = xc * np.hanning(x.shape[0])[:, None]          # hann 抑制泄漏裙边（对拍 seed42 暴露的问题）
    spec = np.abs(np.fft.rfft(xc, n=n_fft, axis=0)) ** 2
    m = _band_mask(n_fft, fs, band)
    ratio = spec[m].sum(axis=0) / (spec.sum(axis=0) + 1e-9)
    rq = np.floor(ratio * 1e4)                    # 量化排序键：消除 libm 末位差异导致的近并列翻转
    idx = sorted(range(x.shape[1]), key=lambda k: (-rq[k], k))[:k]   # 平手按索引升序（与 C 一致）
    idx = np.asarray(idx)
    return x[:, idx].mean(axis=1), idx


def pca_pc1(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """PCA 第一主成分得分（Serene 思路；v1.1：服务器算好权重下发端上）。"""
    xc = x - x.mean(axis=0)
    _, _, vt = np.linalg.svd(xc, full_matrices=False)
    w = vt[0]
    return xc @ w, w


# ---------- ⑤ 带通 ----------

def bandpass(x: np.ndarray, fs: float, band=BREATH_BAND, order: int = 4) -> np.ndarray:
    """双通零相位（正向 sosfilt → 反转 → 再滤 → 反转）。
    与 C 端完全同构（不用 scipy.sosfiltfilt 的奇延拓，保证对拍一致）。"""
    sos = butter(order, band, btype="bandpass", fs=fs, output="sos")
    y = sosfilt(sos, x)
    return sosfilt(sos, y[::-1])[::-1]


# ---------- ⑥ 呼吸率估计（30s 滑窗 FFT + 自相关交叉验证） ----------

def _parabolic(y: np.ndarray, i: int) -> float:
    if 0 < i < len(y) - 1:
        den = y[i - 1] - 2 * y[i] + y[i + 1]
        if abs(den) > 1e-12:
            return i + 0.5 * (y[i - 1] - y[i + 1]) / den
    return float(i)


@dataclass
class BreathResult:
    t: np.ndarray = field(default_factory=lambda: np.array([]))    # 窗中心时刻 s
    bpm_fft: np.ndarray = field(default_factory=lambda: np.array([]))
    bpm_ac: np.ndarray = field(default_factory=lambda: np.array([]))
    agree: np.ndarray = field(default_factory=lambda: np.array([], dtype=bool))
    peak_ratio: np.ndarray = field(default_factory=lambda: np.array([]))  # FFT 峰值/带内均值
    power: np.ndarray = field(default_factory=lambda: np.array([]))       # 短窗功率（门控用）
    valid: np.ndarray = field(default_factory=lambda: np.array([], dtype=bool))
    candidate: np.ndarray = field(default_factory=lambda: np.array([], dtype=bool))  # ⑧ 宽进候选

    @property
    def bpm(self) -> np.ndarray:
        """有效窗口取 FFT/AC 均值，无效窗口 NaN。"""
        v = np.where(self.valid, (self.bpm_fft + self.bpm_ac) / 2.0, np.nan)
        return v


def estimate_breath(x: np.ndarray, fs: float, win_s: float = 30.0, step_s: float = 1.0,
                    nfft: int = 512) -> BreathResult:
    res = BreathResult()
    win, step = int(win_s * fs), int(step_s * fs)
    band = BREATH_BAND
    if len(x) < win:
        return res
    ts, fft_bpm, ac_bpm, agree, ratio = [], [], [], [], []
    for start in range(0, len(x) - win + 1, step):
        seg = x[start:start + win]
        seg = seg * np.hanning(len(seg))
        sp = np.abs(np.fft.rfft(seg, n=nfft))
        fb = np.fft.rfftfreq(nfft, 1.0 / fs)
        m = (fb >= band[0]) & (fb <= band[1])
        spb = sp[m]
        # 相对量化（基准=带内最大）：吸收 FFT 累加次序差（~1e-12 相对），并列取低 bin（与 C 同规）
        ipk = int(np.argmax(np.floor(spb / (spb.max() + 1e-300) * 1e10 + 0.5)))
        frac = _parabolic(sp[m], ipk)              # 带内小数 bin（抛物线插值）
        f_fft = fb[m][0] + frac * (fb[1] - fb[0])  # 带连续，直接换算频率
        fft_bpm.append(f_fft * 60.0)

        ac_seg = x[start:start + win] - x[start:start + win].mean()
        ac = np.correlate(ac_seg, ac_seg, "full")[len(ac_seg) - 1:]
        ac /= ac[0] + 1e-12
        lo, hi = int(fs / band[1]), int(fs / band[0])          # 滞后样本区间
        lo, hi = max(lo, 2), min(hi, len(ac) - 1)
        acs = ac[lo:hi + 1]
        iac = int(np.argmax(np.floor(acs / (acs.max() + 1e-300) * 1e10 + 0.5)))   # 相对量化并列取低 lag（与 C 同规）
        lag = lo + _parabolic(acs, iac)
        ac_bpm.append(60.0 * fs / lag)

        agree.append(abs(f_fft - fs / lag) <= max(0.02, 0.06 * f_fft))
        ratio.append(sp[m][ipk] / (sp[m].mean() + 1e-12))
        ts.append((start + win / 2) / fs)

    res.t = np.asarray(ts)
    res.bpm_fft = np.asarray(fft_bpm)
    res.bpm_ac = np.asarray(ac_bpm)
    res.agree = np.asarray(agree, dtype=bool)
    res.peak_ratio = np.asarray(ratio)
    return res


# ---------- ⑦ 体动门控 ----------

def apply_motion_gate(res: BreathResult, x_filt: np.ndarray, fs: float,
                      short_win_s: float = 7.5,
                      floor: float | None = None, floor_pct: float = 50.0,
                      motion_k: float = 8.0, low_k: float = 0.3,
                      min_peak_ratio: float = 3.5) -> BreathResult:
    """两级门控（Vital-Radio 准静态窗 + Serene 短窗功率）：
      a) 7.5s 短窗功率：超过 floor×motion_k 判体动（挂起），低于 floor×low_k 判无信号
      b) FFT 峰值 ≥ min_peak_ratio × 带内谱均值才判有效
    min_peak_ratio 标定史：5.0（合成）→ 3.5（2026-10-05 桌面/床标定，**验收采用**：
    MAE 0.46/0.78、走动挂起 97% 双达标）→ 3.0 曾试验（覆盖率 26%→54% 翻倍、精度不变，
    但走动挂起降到 84% 破 90% 线、离链站立出现 12% 疑似窗，**已回退**，留作覆盖模式选项）。
    覆盖率 vs 特异性的根本解法：用真实体动数据重标定功率门控 + 相干性选载波（见 M2b 报告 §4）。
    floor=None 时用功率序列的 floor_pct 分位自标定（部署期应使用 ≥10min 无人基线，见方案 §6 校准规程）。"""
    n = max(3, int(short_win_s * fs))
    k = np.ones(n) / n
    pw = np.convolve(x_filt ** 2, k, mode="same")
    idx = np.clip((res.t * fs).astype(int), 0, len(pw) - 1)   # 采样到窗口中心
    res.power = pw[idx]
    base = np.percentile(pw, floor_pct) if floor is None else floor
    res.valid = ((res.power < base * motion_k) & (res.power > base * low_k)
                 & (res.peak_ratio >= min_peak_ratio) & res.agree)
    return res


# ---------- ⑧ 相干轨迹确认（宽进严出，最终裁决） ----------

def coherence_confirm(res: BreathResult,
                      ratio_wide: float = 2.0,
                      power_spike_k: float = 3.0,
                      win_s: float = 90.0,
                      tol_bpm: float = 0.5,
                      min_nb: int = 5) -> BreathResult:
    """侧卧/覆盖率问题的正解（2026-10-05 原型验证、真值闭环 MAE 0.30）。

    宽进：FFT/自相关两法一致 + 峰值比 ≥ ratio_wide + 功率非尖峰（< P75×k，剔除走动突发）
    严出：90s 邻域内候选 ≥ min_nb 且本窗估计与邻域中位差 ≤ tol_bpm
    物理依据：呼吸是单一生理源——真信号窗锁定同一条频率轨迹；走动/干扰的伪有效窗
    散乱无轨迹，被严出自然淘汰。逐窗标量无法区分的场景（弱呼吸 vs 走动，实测比值
    2.05 vs 2.15 几乎重合）在此维度上彻底分离（实测 19%/70% vs 0%）。

    时间轴均匀（步进 1s）→ 邻域用索引窗口 [i±win_s/2]。res.power 由
    apply_motion_gate 填充，须先调用。"""
    n = len(res.t)
    if n == 0 or len(res.power) != n:
        return res
    est = (res.bpm_fft + res.bpm_ac) / 2.0
    p75 = float(np.percentile(res.power, 75))
    res.candidate = res.agree & (res.peak_ratio >= ratio_wide) & (res.power < p75 * power_spike_k)
    half = int(win_s / 2)
    confirmed = np.zeros(n, dtype=bool)
    for i in range(n):
        if not res.candidate[i]:
            continue
        lo, hi = max(0, i - half), min(n, i + half + 1)
        nb = res.candidate[lo:hi]
        if int(nb.sum()) >= min_nb:
            med = float(np.median(est[lo:hi][nb]))
            confirmed[i] = abs(est[i] - med) <= tol_bpm
    res.valid = confirmed
    return res

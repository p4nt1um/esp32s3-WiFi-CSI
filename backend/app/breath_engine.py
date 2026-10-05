"""服务器端呼吸引擎：接收 10Hz 幅度数据，每 10s 出一次呼吸率。

管线（从 csi_pipeline 移植，幅度-only 简化版）：
  ① 缓冲 60s 滚动窗
  ② Hampel 去野点（窗 101@10Hz, β=3）
  ③ 去 DC（10s 滑窗均值）
  ④ top-K=8 呼吸频段 SNR 子载波聚合
  ⑤ 带通 0.1–0.6Hz Butterworth 4 阶
  ⑥ 30s 滑窗 FFT 峰值 + 自相关交叉验证
  ⑦ 功率门控 + 相干轨迹确认
"""
from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np
from scipy.signal import butter, sosfilt

BREATH_BAND = (0.1, 0.6)   # Hz
FS = 10.0                   # 采样率
WIN_S = 30.0                # 分析窗
BUFFER_S = 60.0             # 滚动缓冲
TOPK = 8
MIN_PEAK_RATIO = 3.5
COH_RATIO_WIDE = 2.0
COH_TOL_BPM = 0.5
COH_MIN_NB = 5


class BreathEngine:
    """线程安全：MQTT 回调写入，定时器/查询线程读取。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._buf_t: deque[float] = deque(maxlen=int(BUFFER_S * FS))
        self._buf_a: deque[np.ndarray] = deque(maxlen=int(BUFFER_S * FS))
        self._last_result: dict | None = None

    def feed(self, t_s: float, amps: list[float]) -> None:
        with self._lock:
            self._buf_t.append(t_s)
            self._buf_a.append(np.array(amps, dtype=np.float64))

    @property
    def last_result(self) -> dict | None:
        with self._lock:
            return self._last_result

    def snapshot(self, seconds: float = 30.0) -> dict:
        """返回最近 N 秒的幅度数据（供 API/瀑布图）。"""
        with self._lock:
            if not self._buf_t:
                return {"t": [], "amp": []}
            t_now = self._buf_t[-1]
            cutoff = t_now - seconds
            ts = []
            amps = []
            for tt, aa in zip(self._buf_t, self._buf_a):
                if tt >= cutoff:
                    ts.append(tt - cutoff)    # 相对时间（0 ~ seconds）
                    amps.append(aa.tolist())
        return {"t": ts, "amp": amps}

    def compute(self) -> dict | None:
        """计算当前呼吸率（调用方自行定时，建议 10s 一次）。"""
        with self._lock:
            if len(self._buf_t) < int(WIN_S * FS):
                return None
            t = np.array(self._buf_t)
            amp = np.array(self._buf_a)   # (N, S)

        n = len(t)
        s = amp.shape[1]

        # 均匀重采样（按时间戳线性插值到 10Hz 网格）
        grid = np.arange(t[0], t[-1], 1.0 / FS)
        amp_u = np.column_stack([np.interp(grid, t, amp[:, k]) for k in range(s)])
        n = len(grid)

        # ② Hampel
        from scipy.ndimage import median_filter
        med = median_filter(amp_u, size=(101, 1), mode="reflect")
        mad = median_filter(np.abs(amp_u - med), size=(101, 1), mode="reflect")
        amp_u = np.where(np.abs(amp_u - med) > 3.0 * (1.4826 * mad + 1e-6), med, amp_u)

        # ③ 去 DC
        k = np.ones(101) / 101
        base = np.apply_along_axis(lambda c: np.convolve(c, k, "same"), 0, amp_u)
        amp_u = amp_u - base

        # 剔除死子载波
        live = amp_u.std(axis=0) > 1e-9
        if live.sum() < 4:
            self._last_result = {"bpm": None, "valid": False, "conf": 0.0, "reason": "dead_subs"}
            return self._last_result
        amp_u = amp_u[:, live]

        # ④ top-K
        n_fft = 1 << int(np.ceil(np.log2(n)))
        xc = (amp_u - amp_u.mean(axis=0)) * np.hanning(n)[:, None]
        spec = np.abs(np.fft.rfft(xc, n=n_fft, axis=0)) ** 2
        f = np.fft.rfftfreq(n_fft, 1.0 / FS)
        m = (f >= BREATH_BAND[0]) & (f <= BREATH_BAND[1])
        ratio = spec[m].sum(axis=0) / (spec.sum(axis=0) + 1e-9)
        rq = np.floor(ratio * 1e4)
        idx = sorted(range(amp_u.shape[1]), key=lambda k_: (-rq[k_], k_))[:TOPK]
        agg = amp_u[:, idx].mean(axis=1)

        # ⑤ 带通
        sos = butter(4, BREATH_BAND, btype="bandpass", fs=FS, output="sos")
        y = sosfilt(sos, agg)
        y = sosfilt(sos, y[::-1])[::-1]

        # ⑥ 30s 滑窗估计
        win = int(WIN_S * FS)
        step = int(1.0 * FS)   # 1s 步进
        if n < win:
            self._last_result = {"bpm": None, "valid": False, "conf": 0.0, "reason": "short"}
            return self._last_result

        results = []
        for start in range(0, n - win + 1, step):
            seg = y[start:start + win] * np.hanning(win)
            sp = np.abs(np.fft.rfft(seg, n=512))
            fb = np.fft.rfftfreq(512, 1.0 / FS)
            bm = (fb >= BREATH_BAND[0]) & (fb <= BREATH_BAND[1])
            spb = sp[bm]
            if len(spb) == 0:
                continue
            ipk = int(np.argmax(spb))
            f_fft = fb[bm][0] + ipk * (fb[1] - fb[0])

            raw = y[start:start + win]
            ac = np.correlate(raw - raw.mean(), raw - raw.mean(), "full")[win - 1:]
            ac /= ac[0] + 1e-12
            lo, hi = int(FS / BREATH_BAND[1]), int(FS / BREATH_BAND[0])
            acs = ac[lo:hi + 1]
            if len(acs) == 0:
                continue
            iac = int(np.argmax(acs))
            f_ac = FS / (lo + iac) if (lo + iac) > 0 else 0

            agree = abs(f_fft - f_ac) <= max(0.02, 0.06 * f_fft)
            ratio_val = float(spb[ipk] / (spb.mean() + 1e-12)) if spb.mean() > 0 else 0
            # 功率（简化：窗内方差）
            pw = float(np.mean(y[start:start + win] ** 2))
            results.append({
                "t": (start + win / 2) / FS,
                "bpm_fft": f_fft * 60,
                "bpm_ac": f_ac * 60,
                "agree": agree,
                "ratio": ratio_val,
                "power": pw,
            })

        if not results:
            self._last_result = {"bpm": None, "valid": False, "conf": 0.0, "reason": "no_windows"}
            return self._last_result

        # ⑦ 相干确认
        pw_arr = np.array([r["power"] for r in results])
        p75 = np.percentile(pw_arr, 75) if len(pw_arr) else 0
        cand = np.array([r["agree"] and r["ratio"] >= COH_RATIO_WIDE and r["power"] < p75 * 3.0
                         for r in results])
        est = np.array([(r["bpm_fft"] + r["bpm_ac"]) / 2 for r in results])
        confirmed = np.zeros(len(results), dtype=bool)
        for i in range(len(results)):
            if not cand[i]:
                continue
            lo_i, hi_i = max(0, i - 45), min(len(results), i + 46)
            nb = cand[lo_i:hi_i]
            if nb.sum() >= COH_MIN_NB:
                med = np.median(est[lo_i:hi_i][nb])
                confirmed[i] = abs(est[i] - med) <= COH_TOL_BPM

        valid_ones = est[confirmed]
        if len(valid_ones) > 0:
            bpm = float(np.median(valid_ones))
            conf = float(len(valid_ones) / len(results))
            self._last_result = {"bpm": round(bpm, 1), "valid": True, "conf": round(conf, 2),
                                 "windows": len(results), "confirmed": int(confirmed.sum())}
        else:
            self._last_result = {"bpm": None, "valid": False, "conf": 0.0,
                                 "windows": len(results), "confirmed": 0,
                                 "reason": "no_coherent"}
        return self._last_result

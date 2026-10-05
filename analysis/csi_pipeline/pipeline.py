"""管线编排 + 瀑布图：parse → clean → aggregate → estimate → gate。"""
from __future__ import annotations

from dataclasses import dataclass, field

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from . import dsp
from .parse import CsiRecording, parse_csv


@dataclass
class PipelineResult:
    rec: CsiRecording
    fs10: float
    amp10: np.ndarray            # (N10, S) 清洗后幅度（10Hz，瀑布图数据）
    aggregate: np.ndarray        # (N10,) 聚合信号
    aggregate_filt: np.ndarray   # (N10,) 带通后聚合信号
    sub_idx: np.ndarray          # 选中的子载波索引
    breath: dsp.BreathResult

    @property
    def median_bpm_valid(self) -> float:
        return float(np.nanmedian(self.breath.bpm))

    def summary(self) -> str:
        b = self.breath
        v = b.valid
        return (f"fs={self.rec.fs:.1f}Hz loss={self.rec.loss_pct:.2f}% sub={self.rec.n_sub} "
                f"win={len(b.t)} valid={v.mean() * 100:.0f}% "
                f"median_bpm={np.nanmedian(b.bpm):.2f} "
                f"top_subcarriers={np.sort(self.sub_idx).tolist()}")


def run_pipeline(path_or_rec: str | CsiRecording, *, fs_out: float = 10.0,
                 agg: str = "topk", topk: int = 8, win_s: float = 30.0,
                 floor: float | None = None, motion_k: float = 8.0) -> PipelineResult:
    rec = parse_csv(path_or_rec) if isinstance(path_or_rec, str) else path_or_rec

    amp = rec.amplitude                                   # 100Hz 域
    # ⓪ 丢包重采样：真实链路丢包使时间轴有洞，先按时间戳插值回均匀网格（合成数据无损包时为无操作近似）
    ts_us, amp = dsp.resample_uniform(rec.ts_us, amp, fs=100.0)
    fs_in = 100.0
    live = amp.std(axis=0) > 1e-9                       # 剔除死子载波（实测 sub0 恒 0）
    amp = amp[:, live]
    amp = dsp.hampel(amp, window=15, n_sigmas=3.0)        # ① Hampel（100Hz 域，窗15=0.15s）
    amp10, fs10 = dsp.downsample(amp, fs_in, fs_out)      # ② 抗混叠降采样
    amp10 = dsp.remove_dc(amp10, fs10)                    # ③ 去DC

    if agg == "pca":                                      # ④ 聚合
        agg_sig, _ = dsp.pca_pc1(amp10)
        sub_idx = np.array([-1])
    else:
        agg_sig, sub_idx = dsp.topk_subcarriers(amp10, fs10, k=topk)

    agg_filt = dsp.bandpass(agg_sig, fs10)                # ⑤ 带通 0.1–0.6Hz
    breath = dsp.estimate_breath(agg_filt, fs10, win_s=win_s)   # ⑥ FFT+AC
    breath = dsp.apply_motion_gate(breath, agg_filt, fs10,      # ⑦ 功率门控（填充 power；其 valid 为旧快速判据）
                                   floor=floor, motion_k=motion_k)
    breath = dsp.coherence_confirm(breath)                      # ⑧ 相干轨迹确认（宽进严出，最终 valid）
    if rec.loss_pct > 20.0:                                     # ⑨ 丢包降级守卫：拥塞/故障链路的重采样伪影
        breath.valid[:] = False                                 #    会形成假相干轨迹（实测拥塞段 11.2bpm vs 真值
                                                                #    7.7），数据降级时挂起输出——垃圾进则不出数
    return PipelineResult(rec=rec, fs10=fs10, amp10=amp10, aggregate=agg_sig,
                          aggregate_filt=agg_filt, sub_idx=sub_idx, breath=breath)


def waterfall(res: PipelineResult, out_png: str, title: str = "CSI waterfall") -> None:
    """清洗后幅度瀑布图（时间 × 子载波）。呼吸在敏感子载波上呈竖向条纹。"""
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), height_ratios=[3, 2])
    n10, s = res.amp10.shape
    t = np.arange(n10) / res.fs10
    im = axes[0].imshow(res.amp10.T, aspect="auto", origin="lower",
                        extent=[t[0], t[-1], 0, s], cmap="viridis",
                        vmin=np.percentile(res.amp10, 2), vmax=np.percentile(res.amp10, 98))
    axes[0].set_ylabel("subcarrier")
    axes[0].set_title(title)
    if res.sub_idx[0] >= 0:
        for k in res.sub_idx:
            axes[0].axhline(k, color="r", lw=0.4, alpha=0.5)
    fig.colorbar(im, ax=axes[0], label="amplitude (cleaned)")

    b = res.breath
    axes[1].plot(b.t, b.bpm, ".-", lw=0.8, ms=2)
    axes[1].set_ylim(0, 45)
    axes[1].set_ylabel("bpm (valid only)")
    axes[1].set_xlabel("time (s)")
    axes[1].set_title(f"breathing rate | valid {b.valid.mean() * 100:.0f}%")
    axes[1].grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)

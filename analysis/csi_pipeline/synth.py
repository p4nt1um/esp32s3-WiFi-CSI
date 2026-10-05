"""合成 CSI 数据发生器：已知呼吸频率/体动段的受控测试源。

物理上并不真实，但统计特性对齐真实现象：
  - 呼吸调制只出现在部分子载波上（Fresnel 敏感区），深度因载波而异
  - 慢漂移 + 白噪声 + 冲激野点（测 Hampel）+ 体动大扰动段（测门控）
输出 esp-csi 兼容 CSV，端到端走 parse→pipeline，等于同时测了解析器。
"""
from __future__ import annotations

import numpy as np

HEADER = ("type,id,mac,rssi,rate,sig_mode,mcs,bandwidth,smoothing,not_sounding,"
          "aggregation,stbc,fec_coding,sgi,noise_floor,ampdu_cnt,channel,"
          "secondary_channel,local_timestamp,ant,sig_len,rx_format,len,first_word,data\n")


def synth_csi(*, duration_s: float = 300.0, fs: int = 100, n_sub: int = 52,
              breath_bpm: float = 15.0, breath_depth: float = 6.0,
              motion_windows: tuple[tuple[float, float], ...] = (),
              motion_level: float = 50.0, spike_rate: float = 0.004,
              noise: float = 3.0, drift: bool = True, seed: int = 7) -> dict:
    """返回 dict(ts_us, seq, rssi, amp)，amp 为 (N, n_sub) 幅度。"""
    rng = np.random.default_rng(seed)
    n = int(duration_s * fs)
    t = np.arange(n) / fs

    base = rng.uniform(80, 130, size=(1, n_sub))                 # 子载波底噪幅度
    f_b = breath_bpm / 60.0
    phase = rng.uniform(0, 2 * np.pi, size=(1, n_sub))
    depth_map = breath_depth * (0.25 + 0.75 * np.abs(np.sin(0.35 * np.arange(n_sub) + 0.7)))
    breath = depth_map * np.sin(2 * np.pi * f_b * t[:, None] + phase)
    amp = base + breath + rng.normal(0, noise, size=(n, n_sub))
    if drift:
        amp = amp + 1.5 * np.sin(2 * np.pi * 0.008 * t[:, None]
                                 + rng.uniform(0, 2 * np.pi, size=(1, n_sub)))

    for (t0, t1) in motion_windows:                              # 体动段：大幅随机游走
        m = (t >= t0) & (t <= t1)
        walk = np.cumsum(rng.normal(0, motion_level / 3, size=(m.sum(), n_sub)), axis=0)
        amp[m] += walk - walk.mean(axis=0, keepdims=True) + rng.normal(0, motion_level * 0.3,
                                                                       size=(m.sum(), n_sub))

    spike_n = int(n * n_sub * spike_rate)                        # 冲激野点（测 Hampel）
    if spike_n:
        ridx = rng.integers(0, n, spike_n)
        cidx = rng.integers(0, n_sub, spike_n)
        amp[ridx, cidx] += rng.choice([-1, 1], spike_n) * rng.uniform(70, 120, spike_n)

    # 分配到 I/Q（固定相位角，测幅度提取 sqrt(I²+Q²)）
    theta = rng.uniform(0, 2 * np.pi, size=(1, n_sub))
    return {
        "ts_us": (np.arange(n) * (1e6 / fs)).astype(np.int64),
        "seq": np.arange(n, dtype=np.uint32),
        "rssi": np.round(-45 + rng.normal(0, 1.5, size=n)).astype(np.int16),
        "amp": amp,
        "I": np.clip(np.round(amp * np.cos(theta)), -127, 127).astype(np.int16),
        "Q": np.clip(np.round(amp * np.sin(theta)), -127, 127).astype(np.int16),
    }


def write_csv(path: str, **synth_kwargs) -> dict:
    """合成并写成 esp-csi 兼容 CSV，返回合成参数记录。"""
    d = synth_csi(**synth_kwargs)
    n, n_sub = d["I"].shape
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(HEADER)
        for k in range(n):
            data = ",".join(str(int(v)) for pair in zip(d["I"][k], d["Q"][k]) for v in pair)
            f.write(f"CSI_DATA,{d['seq'][k]},1a:0:0:0:0:0,{d['rssi'][k]},1,1,1,1,0,1,0,0,0,0,-98,1,11,0,"
                    f"{d['ts_us'][k]},0,120,0,{n_sub * 2},0,\"[{data}]\"\n")
    return d

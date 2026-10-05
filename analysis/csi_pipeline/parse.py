"""解析 esp-csi 兼容 CSV（csi_recv / rx_collector 输出格式）。"""
from __future__ import annotations

import csv
from dataclasses import dataclass

import numpy as np

CSI_HEADER_FIRST = "type,id,mac"


@dataclass
class CsiRecording:
    ts_us: np.ndarray      # (N,) local_timestamp 微秒
    seq: np.ndarray        # (N,) TX 载荷序号（丢包率统计）
    rssi: np.ndarray       # (N,) dBm
    iq: np.ndarray         # (N, S) 增益补偿后的 int8 I/Q 交错值
    first_word_invalid: np.ndarray  # (N,) 0/1
    fs: float              # 实测采样率 Hz（时间戳差分中位数）

    @property
    def n_sub(self) -> int:
        return self.iq.shape[1] // 2

    @property
    def amplitude(self) -> np.ndarray:
        """(N, S) 每子载波幅度 sqrt(I^2+Q^2) —— 本项目以幅度为主（方案 §1.3）。"""
        i = self.iq[:, 0::2].astype(np.float64)
        q = self.iq[:, 1::2].astype(np.float64)
        return np.hypot(i, q)

    @property
    def loss_pct(self) -> float:
        """按 TX 序号差分估算丢包率（序号回绕按 2^32 处理）。"""
        if len(self.seq) < 2:
            return 0.0
        d = np.diff(self.seq.astype(np.int64))
        d = np.where(d < 0, d + 2**32, d)
        missing = np.clip(d - 1, 0, None).sum()
        return 100.0 * missing / (missing + len(self.seq))


def parse_csv(path: str, max_rows: int | None = None) -> CsiRecording:
    """读取 esp-csi CSV。容忍乱序/缺字段行（取 len 字段一致的多数行）。"""
    ts, seq, rssi, rows, fwi = [], [], [], [], []
    n_sub_expected = 0
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.reader(f)
        for fields in reader:
            if not fields or fields[0] != "CSI_DATA":
                continue  # 跳过头部/日志行
            try:
                seq.append(int(fields[1]))
                rssi.append(int(fields[3]))
                ts.append(int(fields[18]))       # local_timestamp(us)
                csi_len = int(fields[22])
                fwi.append(int(fields[23]))
                data = fields[24]
                vals = [int(v) for v in data.strip().strip('"[]').split(",") if v != ""]
            except (ValueError, IndexError):
                continue
            if len(vals) < 2:
                continue
            if n_sub_expected == 0:
                n_sub_expected = len(vals) // 2 * 2
            if len(vals) >= n_sub_expected:
                rows.append(vals[:n_sub_expected])
            if max_rows and len(rows) >= max_rows:
                break

    if not rows:
        raise ValueError(f"no CSI_DATA rows parsed from {path}")

    iq = np.asarray(rows, dtype=np.int16)
    ts_us = np.asarray(ts[: len(iq)], dtype=np.int64)
    # 时间戳回绕/异常时退化为标称 100Hz
    dt = np.diff(ts_us)
    dt_ok = dt[(dt > 1000) & (dt < 10_000_000)]
    fs = float(np.median(dt_ok)) ** -1 * 1e6 if len(dt_ok) > 10 else 100.0

    return CsiRecording(
        ts_us=ts_us,
        seq=np.asarray(seq[: len(iq)], dtype=np.uint32),
        rssi=np.asarray(rssi[: len(iq)], dtype=np.int16),
        iq=iq,
        first_word_invalid=np.asarray(fwi[: len(iq)], dtype=np.uint8),
        fs=fs,
    )

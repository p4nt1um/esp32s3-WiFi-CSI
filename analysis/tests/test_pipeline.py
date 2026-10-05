"""合成数据端到端验证（无需硬件）。

运行：.venv/Scripts/python.exe tests/test_pipeline.py
判定标准来自《项目实施方案》§1.1 验收指标在黄金实现上的投影。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from csi_pipeline.pipeline import run_pipeline, waterfall
from csi_pipeline.synth import write_csv

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, cond: bool, detail: str) -> None:
    results.append((PASS if cond else FAIL, name, detail))


def run_case(name: str, *, bpm: float, motion=(), duration=300.0, seed=7,
             expect_bpm=True) -> object:
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, dir=".") as f:
        csv_path = f.name
    try:
        write_csv(csv_path, duration_s=duration, breath_bpm=bpm,
                  motion_windows=motion, seed=seed)
        return run_pipeline(csv_path)
    finally:
        Path(csv_path).unlink(missing_ok=True)


def t1_quiet_15bpm():
    res = run_case("quiet", bpm=15)
    b = res.breath
    med = np.nanmedian(b.bpm)
    check("t1 呼吸率精度(15bpm)", abs(med - 15) <= 1.0, f"median={med:.2f}bpm")
    check("t1 有效率", b.valid.mean() > 0.85, f"valid={b.valid.mean() * 100:.0f}%")
    check("t1 采样率解析", abs(res.rec.fs - 100) < 2, f"fs={res.rec.fs:.1f}")
    check("t1 子载波数", res.rec.n_sub == 52, f"n_sub={res.rec.n_sub}")
    check("t1 丢包率", res.rec.loss_pct < 1, f"loss={res.rec.loss_pct:.2f}%")


def t2_motion_gating():
    motion = ((120, 150),)
    res = run_case("motion", bpm=15, motion=motion, seed=11)
    b = res.breath
    t0, t1 = motion[0]
    in_m = (b.t > t0 + 5) & (b.t < t1 - 5)
    out_m = ((b.t < t0 - 20) | (b.t > t1 + 20)) & (b.t > 35)
    gated = ~b.valid[in_m]
    kept = b.valid[out_m]
    check("t2 体动段挂起", gated.mean() > 0.8, f"gated={gated.mean() * 100:.0f}%")
    check("t2 静止段保持", kept.mean() > 0.8, f"kept={kept.mean() * 100:.0f}%")
    check("t2 静止段精度", abs(np.nanmedian(b.bpm[out_m]) - 15) <= 1.5,
          f"median={np.nanmedian(b.bpm[out_m]):.2f}")


def t3_other_rate():
    res = run_case("21bpm", bpm=21, seed=23)
    med = np.nanmedian(res.breath.bpm)
    check("t3 呼吸率精度(21bpm)", abs(med - 21) <= 1.0, f"median={med:.2f}")


def t4_ht40_64sub():
    res = run_case("ht40", bpm=15, seed=5)
    # 直接构造 64 子载波解析测试
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, dir=".") as f:
        p = f.name
    try:
        write_csv(p, duration_s=120, breath_bpm=15, n_sub=64, seed=5)
        r64 = run_pipeline(p)
        check("t4 64子载波解析", r64.rec.n_sub == 64, f"n_sub={r64.rec.n_sub}")
    finally:
        Path(p).unlink(missing_ok=True)


def t5_waterfall_png():
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, dir=".") as f:
        p = f.name
    png = p.replace(".csv", ".png")
    try:
        write_csv(p, duration_s=180, breath_bpm=15, motion_windows=((90, 110),), seed=3)
        res = run_pipeline(p)
        waterfall(res, png)
        check("t5 瀑布图生成", Path(png).stat().st_size > 30_000,
              f"{Path(png).stat().st_size // 1024}KB")
    finally:
        Path(p).unlink(missing_ok=True)
        Path(png).unlink(missing_ok=True)


def t6_pca_agg():
    res = run_case("pca", bpm=15, seed=9)
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, dir=".") as f:
        p = f.name
    try:
        write_csv(p, duration_s=300, breath_bpm=15, seed=9)
        rp = run_pipeline(p, agg="pca")
        med = np.nanmedian(rp.breath.bpm)
        check("t6 PCA聚合精度", abs(med - 15) <= 1.2, f"median={med:.2f}")
    finally:
        Path(p).unlink(missing_ok=True)


def t7_weak_signal_coherence():
    """弱信号（浅呼吸+高底噪，模拟侧卧实测形态）：旧逐窗门控比值不足，
    ⑧相干轨迹确认应恢复出准确轨迹（实测原型：旧 0% → 相干恢复且 MAE 达标）。"""
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, dir=".") as f:
        p = f.name
    try:
        write_csv(p, duration_s=300, breath_bpm=15, breath_depth=2.0, noise=8.0, seed=31)
        r = run_pipeline(p)
        b = r.breath
        med = np.nanmedian(b.bpm)
        legacy_valid = b.candidate & (b.peak_ratio >= 3.5)
        check("t7 弱信号旧门控失效", legacy_valid.mean() < 0.10,
              f"legacy_valid={legacy_valid.mean() * 100:.0f}%")
        check("t7 相干确认恢复", b.valid.mean() > 0.04 and b.valid.sum() >= 10,
              f"valid={b.valid.mean() * 100:.0f}% ({int(b.valid.sum())} 窗)")
        check("t7 弱信号精度", abs(med - 15) <= 1.5, f"median={med:.2f}")
    finally:
        Path(p).unlink(missing_ok=True)


if __name__ == "__main__":
    for fn in (t1_quiet_15bpm, t2_motion_gating, t3_other_rate,
               t4_ht40_64sub, t5_waterfall_png, t6_pca_agg, t7_weak_signal_coherence):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            results.append((FAIL, fn.__name__, f"exception: {e}"))

    w = max(len(r[1]) for r in results)
    n_fail = 0
    for status, name, detail in results:
        print(f"[{status}] {name:<{w}}  {detail}")
        n_fail += status == FAIL
    print(f"\n{n_fail} failed / {len(results)} total")
    sys.exit(1 if n_fail else 0)

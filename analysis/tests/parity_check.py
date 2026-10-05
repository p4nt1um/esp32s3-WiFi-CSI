"""M2a 对拍：C 端 csi_pc（float32）vs Python 黄金实现（float64）。

验收（方案 §5 M2a）：同一 CSV 输入，逐窗 bpm 差 ≤ 0.2 bpm（双有效窗），
体动/有效判定一致率 ≥ 99%。

运行：.venv/Scripts/python.exe tests/parity_check.py [csi_pc.exe 路径]
"""
from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CSI_PC = Path(sys.argv[1]) if len(sys.argv) > 1 else \
    ROOT.parent / "firmware" / "components" / "csi_core" / "build" / "csi_pc.exe"
PY = sys.executable

TOL_BPM = 0.2
TOL_FLAG = 0.99

CASES = [
    ("quiet15", dict(duration_s=300, breath_bpm=15, seed=7)),
    ("motion15", dict(duration_s=300, breath_bpm=15, seed=11,
                      motion_windows=((120, 150),))),
    ("bpm21", dict(duration_s=300, breath_bpm=21, seed=23)),
    ("quiet15b", dict(duration_s=240, breath_bpm=15, seed=42)),
]


def read_windows(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> int:
    if not CSI_PC.exists():
        print(f"csi_pc not found: {CSI_PC}")
        print("build: cd firmware/components/csi_core && "
              "analysis/.venv/Scripts/python.exe -m ziglang cc -O2 -Iinclude "
              "src/csi_core.c pc_main.c -lm -o build/csi_pc.exe")
        return 2

    n_fail = 0
    with tempfile.TemporaryDirectory(dir=".") as td:
        for name, kw in CASES:
            c_csv = str(Path(td) / f"{name}.csv")
            c_win = str(Path(td) / f"{name}_c.csv")
            p_win = str(Path(td) / f"{name}_py.csv")

            from csi_pipeline.synth import write_csv
            write_csv(c_csv, **kw)

            r = subprocess.run([str(CSI_PC), c_csv, c_win], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"[FAIL] {name}: csi_pc rc={r.returncode} {r.stderr.strip()}")
                n_fail += 1
                continue

            r = subprocess.run([PY, "-m", "csi_pipeline", "breath", c_csv,
                                "--dump-windows", p_win],
                               capture_output=True, text=True, cwd=str(ROOT))
            if r.returncode != 0:
                print(f"[FAIL] {name}: python rc={r.returncode} {r.stderr.strip()}")
                n_fail += 1
                continue

            cw, pw = read_windows(c_win), read_windows(p_win)
            if len(cw) != len(pw):
                print(f"[FAIL] {name}: window count {len(cw)} vs {len(pw)}")
                n_fail += 1
                continue

            flag_agree = sum(a["valid"] == b["valid"] for a, b in zip(cw, pw)) / len(cw)
            both = [(a, b) for a, b in zip(cw, pw) if a["valid"] == "1" and b["valid"] == "1"]
            max_diff = max((abs(float(a["bpm"]) - float(b["bpm"])) for a, b in both), default=0.0)
            med_c = sorted(float(a["bpm"]) for a, b in both)[len(both) // 2] if both else float("nan")

            ok = flag_agree >= TOL_FLAG and max_diff <= TOL_BPM
            n_fail += not ok
            print(f"[{'PASS' if ok else 'FAIL'}] {name}: 窗数={len(cw)} "
                  f"判定一致率={flag_agree * 100:.1f}% 双有效={len(both)} "
                  f"max|Δbpm|={max_diff:.3f} C中位bpm={med_c:.2f}")

    print(f"\n{n_fail} failed / {len(CASES)} cases")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())

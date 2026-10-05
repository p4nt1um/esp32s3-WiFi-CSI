"""命令行入口：synth / breath / waterfall / full。"""
from __future__ import annotations

import argparse
import sys

from .pipeline import run_pipeline, waterfall
from .synth import write_csv


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="csi_pipeline",
                                description="WiFi CSI 呼吸监测分析管线（M1）")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("synth", help="生成合成 CSI CSV")
    ps.add_argument("out_csv")
    ps.add_argument("--duration", type=float, default=300)
    ps.add_argument("--bpm", type=float, default=15)
    ps.add_argument("--fs", type=int, default=100)
    ps.add_argument("--n-sub", type=int, default=52)
    ps.add_argument("--motion", type=str, default="",
                    help="体动段，如 120:150,200:230")
    ps.add_argument("--seed", type=int, default=7)

    pb = sub.add_parser("breath", help="对 CSV 跑呼吸管线并打印结果")
    pb.add_argument("csv")
    pb.add_argument("--agg", choices=["topk", "pca"], default="topk")
    pb.add_argument("--floor", type=float, default=None, help="噪声基线（默认自标定）")
    pb.add_argument("--win", type=float, default=30)
    pb.add_argument("--dump-windows", type=str, default=None,
                    help="逐窗结果导出 CSV（与 C 端 csi_pc 同格式，用于对拍）")

    pw = sub.add_parser("waterfall", help="生成瀑布图 PNG")
    pw.add_argument("csv")
    pw.add_argument("out_png")
    pw.add_argument("--agg", choices=["topk", "pca"], default="topk")

    args = p.parse_args(argv)

    if args.cmd == "synth":
        motion = tuple(tuple(map(float, w.split(":"))) for w in args.motion.split(",") if w)
        write_csv(args.out_csv, duration_s=args.duration, fs=args.fs,
                  breath_bpm=args.bpm, n_sub=args.n_sub,
                  motion_windows=motion, seed=args.seed)
        print(f"written {args.out_csv} ({args.duration}s @ {args.fs}Hz, "
              f"{args.bpm}bpm, motion={motion or 'none'})")
        return 0

    res = run_pipeline(args.csv, agg=args.agg, win_s=getattr(args, "win", 30),
                       floor=getattr(args, "floor", None))
    print(res.summary())
    b = res.breath
    if getattr(args, "dump_windows", None):
        with open(args.dump_windows, "w", encoding="utf-8") as f:
            f.write("t,bpm_fft,bpm_ac,agree,peak_ratio,power,valid,bpm\n")
            for i in range(len(b.t)):
                bpm = "" if not b.valid[i] else f"{b.bpm[i]:.6g}"
                f.write(f"{b.t[i]:.6g},{b.bpm_fft[i]:.6g},{b.bpm_ac[i]:.6g},"
                        f"{int(b.agree[i])},{b.peak_ratio[i]:.6g},{b.power[i]:.6g},"
                        f"{int(b.valid[i])},{bpm if bpm else 'nan'}\n")
    for i in range(0, len(b.t), max(1, len(b.t) // 12)):
        print(f"  t={b.t[i]:7.1f}s bpm={b.bpm[i]:5.1f} valid={int(b.valid[i])} "
              f"ratio={b.peak_ratio[i]:6.1f}")
    if args.cmd == "waterfall":
        waterfall(res, args.out_png, title=f"CSI waterfall | {args.csv}")
        print(f"written {args.out_png}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

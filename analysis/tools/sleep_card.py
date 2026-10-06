"""智能手表风格睡眠卡片：把 breath 表整夜数据画成消费者可读图。

分期为呼吸代理推断（非医学睡眠分期，图上带脚注）：
  深睡代理 = 呼吸率 < SLOW_BPM 且有效（慢而稳）
  REM 代理 = 呼吸率 >= REM_BPM 且有效（快）
  浅睡代理 = 其余有效窗（基线呼吸）
  清醒    = 入睡前 + 长无效段（>=2min）
  短无效段沿用前一阶段（侧卧弱信号大概率仍在睡），另设"信号质量"细条保持诚实
翻身 = 睡眠期(入睡点后) >=2min 挂起；起夜 = 长无效/数据缺口 + 觉醒性呼吸簇

用法: analysis/.venv/Scripts/python.exe analysis/tools/sleep_card.py --from "..." --to "..."
输出: analysis/output/sleep_card_<日期>.png
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
from pathlib import Path

import numpy as np

SLOW_BPM = 7.5     # 深睡代理线
REM_BPM = 9.5      # REM 代理线（睡眠期内的较快呼吸）
AWAKE_MIN_S = 120  # 长无效段 → 清醒
SMOOTH_WIN = 6     # 多数票平滑（×10s = 1min）

# 手表风配色
C_AWAKE, C_REM, C_LIGHT, C_DEEP, C_GAP = "#f59e0b", "#10b981", "#60a5fa", "#1e40af", "#d4d4d4"
STAGES = ["清醒", "REM*", "浅睡*", "深睡*", "数据缺失"]


def fmt_ts(t: float) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%H:%M")


def runs_of(mask: np.ndarray) -> list[tuple[int, int]]:
    out, i = [], 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j < len(mask) and mask[j]:
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).resolve().parents[2] / "backend" / "csi.db"))
    ap.add_argument("--device", default="rx1")
    ap.add_argument("--from", dest="t0", required=True)
    ap.add_argument("--to", dest="t1", required=True)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "output"))
    args = ap.parse_args()
    t0 = dt.datetime.strptime(args.t0, "%Y-%m-%d %H:%M")
    t1 = dt.datetime.strptime(args.t1, "%Y-%m-%d %H:%M")

    db = sqlite3.connect(args.db)
    rows = db.execute("SELECT ts, bpm, valid FROM breath WHERE device=? AND ts>=? AND ts<? ORDER BY ts",
                      (args.device, int(t0.timestamp()), int(t1.timestamp()))).fetchall()
    if len(rows) < 60:
        raise SystemExit("数据不足")

    ts = np.array([r[0] for r in rows], float)
    bpm = np.array([r[1] if r[1] is not None else np.nan for r in rows])
    valid = np.array([r[2] for r in rows]) == 1

    # 会话窗口：最后一个 >10min 缺口之后（夜间折腾/事故段结束）到末尾
    gaps = [(i, ts[i + 1] - ts[i]) for i in range(len(ts) - 1) if ts[i + 1] - ts[i] > 600]
    s = gaps[-1][0] + 1 if gaps else 0
    ts, bpm, valid = ts[s:], bpm[s:], valid[s:]

    # 入睡点：稳定降至 9.5 下并保持 5 窗
    onset = None
    for i in range(len(bpm) - 5):
        if valid[i:i + 5].all() and np.nanmax(bpm[i:i + 5]) < 9.5:
            onset = i
            break

    # 长无效段 → 清醒（睡眠期内 ≥2min）
    inv = ~valid
    awake_extra = np.zeros(len(ts), bool)
    for a, b in runs_of(inv):
        if (b - a) >= 3 and (ts[min(b, len(ts) - 1)] - ts[a]) >= AWAKE_MIN_S:
            awake_extra[a:b] = True

    # 分期
    stage = np.full(len(ts), 3, int)          # 0清醒 1REM 2浅睡 3深睡 4缺失
    for i in range(len(ts)):
        if onset is not None and i < onset:
            stage[i] = 0                       # 入睡前在床
        elif awake_extra[i]:
            stage[i] = 0
        elif not valid[i]:
            stage[i] = 4 if False else stage[max(i - 1, 0)] if i else 0   # 短挂起沿用前值
        elif bpm[i] < SLOW_BPM:
            stage[i] = 3
        elif bpm[i] >= REM_BPM:
            stage[i] = 1
        else:
            stage[i] = 2
    # 多数票平滑（1min）——必须在副本上做：就地修改会让前段值向后逐窗渗透污染
    prev = stage.copy()
    for i in range(len(stage)):
        lo = max(0, i - SMOOTH_WIN // 2)
        hi = min(len(stage), lo + SMOOTH_WIN)
        stage[i] = np.bincount(prev[lo:hi], minlength=5).argmax()

    # 事件：翻身（睡眠期 ≥2min 挂起）、起夜（清醒段 + 觉醒呼吸）
    cut = onset if onset is not None else 0
    turnovers = []
    for a, b in runs_of(inv):
        dur = ts[min(b, len(ts) - 1)] - ts[a]
        if a >= cut and dur >= AWAKE_MIN_S and (b - a) >= 3:
            turnovers.append((ts[a], dur))
    # 起夜：睡眠期内长挂起 + 附近出现 >14bpm
    bathroom = []
    for t_ev, d in turnovers:
        m = (np.abs(ts - t_ev) < 300) & valid & (bpm > 14)
        if m.any():
            bathroom.append(t_ev)

    # ---------- 画图 ----------
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle, FancyBboxPatch
    plt.rcParams["font.family"] = "Microsoft YaHei"
    plt.rcParams["axes.unicode_minus"] = False

    hh = (ts - ts[0]) / 3600
    fig = plt.figure(figsize=(13, 7.2), facecolor="white")
    gs = fig.add_gridspec(3, 1, height_ratios=[2.5, 1.6, 0.55], hspace=0.30,
                          left=0.10, right=0.97, top=0.80, bottom=0.13)
    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1], sharex=ax1)
    ax3 = fig.add_subplot(gs[2], sharex=ax1)

    # 顶部：分期色带（每窗一竖条）
    colors = {0: C_AWAKE, 1: C_REM, 2: C_LIGHT, 3: C_DEEP, 4: C_GAP}
    for st, c in colors.items():
        m = stage == st
        if m.any():
            ax1.bar(hh[m], 1.0, width=10 / 3600, bottom=st, color=c, align="edge",
                    edgecolor="none")
    ax1.set_yticks([v + 0.5 for v in range(5)], STAGES)
    ax1.set_ylim(0, 5)
    ax1.grid(axis="x", alpha=.25)
    ax1.set_title(f"昨晚睡眠 — {dt.datetime.fromtimestamp(ts[0]):%m-%d %H:%M} 至 {fmt_ts(ts[-1])}",
                  fontsize=15, loc="left", pad=12)

    # 事件标注
    if onset is not None:
        ax1.axvline(hh[onset], color="#2563eb", lw=1.2, ls="--")
        ax1.text(hh[onset] + 0.03, 5.45, f"入睡 {fmt_ts(ts[onset])}", fontsize=10,
                 color="#2563eb", va="top")
    wake_i = len(ts) - 1
    ax1.axvline(hh[-1], color="#6b7280", lw=1.2, ls="--")
    ax1.text(hh[-1] - 0.05, 5.45, f"结束 {fmt_ts(ts[-1])}", fontsize=10, color="#6b7280",
             va="top", ha="right")
    for t_ev, d in turnovers:
        x = (t_ev - ts[0]) / 3600
        if t_ev in bathroom:
            ax1.annotate("起夜(疑似)", xy=(x, 4.6), xytext=(x - 0.4, 5.3), fontsize=10,
                         color="#dc2626", ha="center",
                         arrowprops=dict(arrowstyle="->", color="#dc2626"))
        else:
            ax1.annotate("翻身", xy=(x, 3.6), xytext=(x, 4.9), fontsize=10, color="#b45309",
                         ha="center", arrowprops=dict(arrowstyle="->", color="#b45309"))

    # 中部：呼吸率曲线（手表绿色呼吸曲线样式）
    ax2.plot(hh, bpm, lw=1.0, color="#10b981")
    ax2.fill_between(hh, bpm, 4, color="#10b981", alpha=.12)
    med = np.nanmedian(bpm[valid])
    ax2.axhline(med, color="#9ca3af", ls=":", lw=.9)
    ax2.text(hh[-1], med + 0.3, f"中位 {med:.1f}", fontsize=9, color="#6b7280", ha="right")
    ax2.set_ylabel("呼吸率 (bpm)", fontsize=10)
    ax2.set_ylim(4, 20)
    ax2.grid(alpha=.25)

    # 底部：信号质量细条（诚实地展示无效段）
    ax3.bar(hh[valid], 1.0, width=10 / 3600, color="#86efac", align="edge")
    ax3.bar(hh[~valid], 1.0, width=10 / 3600, color="#e5e7eb", align="edge")
    ax3.set_yticks([0.5], ["信号"])
    ax3.set_ylim(0, 1)

    ticks = np.arange(0, hh[-1] + 0.01, 0.5)
    ax3.set_xticks(ticks)
    ax3.set_xticklabels([fmt_ts(ts[0] + t * 3600) for t in ticks], fontsize=9)
    plt.setp(ax1.get_xticklabels(), visible=False)
    plt.setp(ax2.get_xticklabels(), visible=False)
    for ax in (ax1, ax2, ax3):
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)

    # 统计卡片（手表风）
    v = valid
    sleep_h = (ts[-1] - (ts[onset] if onset is not None else ts[0])) / 3600
    n_sleep = max(len(ts) - cut, 1)          # 睡眠期窗数（入睡点后）
    stats = [
        ("睡眠时长", f"{sleep_h:.1f} 小时"),
        ("入睡用时", f"{(ts[onset]-ts[0])/60:.0f} 分钟" if onset is not None else "—"),
        ("深睡*", f"{np.mean(stage[cut:]==3)*100:.0f}% ≈ {np.sum(stage[cut:]==3)*10/60:.0f} 分钟"),
        ("REM*", f"{np.sum(stage[cut:]==1)*10/60:.0f} 分钟"),
        ("翻身", f"{len(turnovers)-len(bathroom)} 次"),
        ("起夜", f"{len(bathroom)} 次(疑似)" if bathroom else "0 次"),
        ("平均呼吸", f"{med:.1f} bpm"),
    ]
    x0, y0, w, h = 0.10, 0.86, 0.125, 0.10
    for i, (k, val) in enumerate(stats):
        xx = x0 + i * w
        fig.patches.append(FancyBboxPatch((xx, y0), w - 0.012, h, boxstyle="round,pad=0.008",
                                          fc="#f9fafb", ec="#e5e7eb", transform=fig.transFigure))
        fig.text(xx + 0.012, y0 + h - 0.028, k, fontsize=9, color="#6b7280")
        fig.text(xx + 0.012, y0 + 0.022, val, fontsize=12, color="#111827", fontweight="bold")

    fig.text(0.10, 0.035, "* 分期为呼吸率代理推断（慢而稳→深睡、快→REM），非医学睡眠分期；短时信号丢失沿用前一阶段，见底部信号条。",
             fontsize=8.5, color="#9ca3af")

    out = Path(args.out) / f"sleep_card_{t0:%Y%m%d}.png"
    Path(args.out).mkdir(parents=True, exist_ok=True)
    plt.savefig(out, dpi=130, facecolor="white")
    print("card:", out)


if __name__ == "__main__":
    main()

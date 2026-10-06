"""睡眠质量报告生成器：从 backend/csi.db 的 breath 表出整夜报告。

用法（在仓库根目录）：
  analysis/.venv/Scripts/python.exe analysis/tools/sleep_report.py            # 自动取最近一晚
  analysis/.venv/Scripts/python.exe analysis/tools/sleep_report.py --from "2026-10-05 20:00" --to "2026-10-06 10:00"
  可选: --device rx1 --db backend/csi.db --out analysis/output

输出: sleep_report_<夜起始日期>.md + .png

依赖逐窗指标列（ratio/band_power/raw_std/loss，2026-10-06 起落库）：
  - 体动/翻身判定升级为"invalid 且 raw_std 升高"（体动型）
  - 呼吸暂停代理：呼吸带功率塌缩(≤30%滚动中位) 且 raw_std 不变(非体动) 且低丢包，
    连续≥3窗(≥30s)。30s 分析窗下限决定了它捕捉 ≥40s 事件；
    医学级 10-30s 呼吸暂停需原始幅度序列（后续升级项）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

import numpy as np

# ---------- 阈值（集中一处，调参后复检特异性） ----------
ONSET_BPM = 9.5          # 入睡线：呼吸率稳定降至该值以下
ONSET_HOLD = 5           # 连续保持窗数（×10s）
TURNOVER_MIN_S = 120     # 大翻身：体动型挂起 ≥2min
MICRO_MIN_S = 40         # 微体动：挂起 40s–2min
AROUSAL_BPM = 14.0       # 觉醒性呼吸阈值
SLOW_BPM = 7.5           # 慢呼吸（深睡代理）
APNEA_POWER_FRAC = 0.30  # 呼吸带功率跌破滚动中位的 30%
APNEA_RAW_MAX = 2.0      # raw_std 不得超滚动中位 2 倍（否则是体动）
APNEA_LOSS_MAX = 0.20    # 丢包守卫
APNEA_MIN_WIN = 3        # 连续 ≥3 窗 = ≥30s
ROLL_MIN = 60            # 滚动基线最少样本（10min）


def rolling_median(x: np.ndarray, win: int = 180) -> np.ndarray:
    """以 win 行（默认 30min）为窗的滚动中位，边缘用可用部分。"""
    out = np.empty_like(x, dtype=float)
    half = win // 2
    for i in range(len(x)):
        lo, hi = max(0, i - half), min(len(x), i + half + 1)
        out[i] = np.median(x[lo:hi])
    return out


def fmt_ts(t: float) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%H:%M")


def fmt_dur(s: float) -> str:
    return f"{int(s // 60)}分{int(s % 60):02d}秒" if s < 3600 else f"{s / 3600:.1f}小时"


def seg_runs(mask: np.ndarray, ts: np.ndarray, max_gap: float = 60.0) -> list[tuple[int, int]]:
    """True 连续段 [start, end)；段内出现 >max_gap 的数据缺口即切断（避免把缺口缝成超长段）。"""
    runs, i = [], 0
    while i < len(mask):
        if mask[i]:
            j = i + 1
            while j < len(mask) and mask[j] and ts[j] - ts[j - 1] <= max_gap:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    return runs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(__file__).resolve().parents[2] / "backend" / "csi.db"))
    ap.add_argument("--device", default="rx1")
    ap.add_argument("--from", dest="t0", default=None, help='起始 "YYYY-MM-DD HH:MM"，默认昨天 18:00')
    ap.add_argument("--to", dest="t1", default=None, help='结束 "YYYY-MM-DD HH:MM"，默认今天 12:00')
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "output"))
    args = ap.parse_args()

    now = dt.datetime.now()
    t0 = (dt.datetime.strptime(args.t0, "%Y-%m-%d %H:%M") if args.t0
          else (now - dt.timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0))
    t1 = (dt.datetime.strptime(args.t1, "%Y-%m-%d %H:%M") if args.t1
          else now.replace(hour=12, minute=0, second=0, microsecond=0))

    db = sqlite3.connect(args.db)
    rows = db.execute(
        "SELECT ts, bpm, valid, ratio, band_power, raw_std, loss FROM breath "
        "WHERE device=? AND ts>=? AND ts<? ORDER BY ts",
        (args.device, int(t0.timestamp()), int(t1.timestamp()))).fetchall()
    if len(rows) < 60:
        sys.exit(f"[{args.device}] {t0}~{t1} 仅 {len(rows)} 行，不足以出报告")

    ts = np.array([r[0] for r in rows], dtype=float)
    bpm = np.array([r[1] if r[1] is not None else np.nan for r in rows])
    valid = np.array([r[2] for r in rows]) == 1
    ratio = np.array([r[3] if r[3] is not None else np.nan for r in rows])
    power = np.array([r[4] if r[4] is not None else np.nan for r in rows])
    raw_std = np.array([r[5] if r[5] is not None else np.nan for r in rows])
    loss = np.array([r[6] if r[6] is not None else np.nan for r in rows])
    has_metrics = np.mean(~np.isnan(power)) >= 0.05   # 指标行覆盖率 ≥5% 才启用分型/暂停代理

    L: list[str] = []
    title = f"睡眠质量报告 — {t0:%Y-%m-%d %H:%M} → {t1:%Y-%m-%d %H:%M}（{args.device}）"
    L += [f"# {title}", ""]

    # ---------- 1. 数据质量 ----------
    gaps = [(ts[i], ts[i + 1] - ts[i]) for i in range(len(ts) - 1) if ts[i + 1] - ts[i] > 60]
    span = ts[-1] - ts[0]
    L += ["## 1. 数据质量", "",
          f"- 记录时长 {fmt_dur(span)}，{len(ts)} 窗（10s/窗），有效窗 {valid.mean()*100:.0f}%",
          f"- 有效监测 {valid.sum()*10/60:.0f} 分钟；数据缺口(>60s) {len(gaps)} 处"
          + (f"：{', '.join(f'{fmt_ts(g[0])} {g[1]/60:.0f}分' for g in gaps[:6])}" if gaps else ""),
          f"- 逐窗指标（体动/功率）：{f'可用（覆盖 {np.mean(~np.isnan(power))*100:.0f}% 行）' if has_metrics else '不可用（早于 2026-10-06 落库，暂停代理与体动分型不可算）'}",
          ""]

    # ---------- 2. 睡眠结构 ----------
    settle = ts[0]
    if gaps:  # 躺定 ≈ 最后一个大缺口之后（夜间折腾结束）
        big = [g for g in gaps if g[1] > 600]
        if big:
            settle = big[-1][0] + big[-1][1]
    onset = None
    for i in range(len(bpm) - ONSET_HOLD + 1):
        if ts[i] < settle:   # 只在躺定后找入睡点（避免傍晚在床/桌旁数据抢先命中）
            continue
        w = valid[i:i + ONSET_HOLD]
        bv = bpm[i:i + ONSET_HOLD]
        if w.all() and np.nanmax(bv) < ONSET_BPM:
            onset = i
            break
    v = valid
    sleep_min = v.sum() * 10 / 60
    L += ["## 2. 睡眠结构", "",
          f"- 躺定参考点 {fmt_ts(settle)}（最后长缺口后）"
          + (f"；入睡代理点 {fmt_ts(ts[onset])}（呼吸率稳定降至 {ONSET_BPM}bpm 下），潜伏期约 {fmt_dur(ts[onset]-settle)}" if onset is not None else "；入睡点未检出"),
          f"- 在床 {fmt_dur(span)}，有效睡眠监测 {sleep_min:.0f} 分钟",
          f"- 时长判定：{'≥7h 正常' if span>=7*3600 else '5–7h 偏短' if span>=5*3600 else '<5h 不足'}（口径为记录窗，非严格睡眠时间）",
          ""]

    # ---------- 3. 呼吸 ----------
    bv = bpm[v]
    L += ["## 3. 呼吸", "",
          f"- 有效窗中位 **{np.nanmedian(bv):.1f} bpm**，IQR {np.nanpercentile(bv,25):.1f}–{np.nanpercentile(bv,75):.1f}",
          f"- 慢呼吸（<{SLOW_BPM}bpm，深睡代理）占 {np.mean(bv<SLOW_BPM)*100:.0f}%；"
          f"觉醒性呼吸（>{AROUSAL_BPM:.0f}bpm）占 {np.mean(bv>AROUSAL_BPM)*100:.1f}%（{np.nansum(bv>AROUSAL_BPM)*10/60:.0f} 分钟）",
          "",
          "| 时段 | 有效率 | bpm 中位 | IQR |", "|---|---|---|---|"]
    t_start = settle if settle > ts[0] else ts[0]
    h = t_start
    while h < ts[-1]:
        m = (ts >= h) & (ts < h + 3600)
        if m.any():
            mv = m & v
            if mv.sum() >= 3:
                bvh = bpm[mv]
                L.append(f"| {fmt_ts(h)}–{fmt_ts(h+3600)} | {v[m].mean()*100:.0f}% | {np.nanmedian(bvh):.1f} | {np.nanpercentile(bvh,25):.1f}–{np.nanpercentile(bvh,75):.1f} |")
            else:
                L.append(f"| {fmt_ts(h)}–{fmt_ts(h+3600)} | {v[m].mean()*100:.0f}% | — | 有效窗不足 |")
        h += 3600
    L.append("")

    # ---------- 4. 体动与觉醒 ----------
    inv = ~v
    # 体动分型：invalid 且 raw_std 高于滚动基线（仅对有指标的行判定，无指标行记 None=未分型）
    if has_metrics:
        rs_fill = np.nanmedian(raw_std)
        rs_base = rolling_median(np.nan_to_num(raw_std, nan=rs_fill))
        typed = (~np.isnan(raw_std)) & (np.nan_to_num(raw_std, nan=0) > 1.5 * rs_base)
    else:
        typed = np.zeros(len(ts), dtype=bool)
    turnovers, micros = [], []
    for a, b in seg_runs(inv, ts):
        dur = ts[min(b, len(ts)-1)] - ts[a]
        if dur < MICRO_MIN_S or (b - a) < 3:
            continue
        after = bpm[b:b+6][v[b:b+6]]
        seg_has_metric = (~np.isnan(raw_std[a:b])).any()
        ev = (ts[a], dur, np.nanmedian(after) if len(after) else np.nan,
              bool(typed[a:b].any()) if seg_has_metric else None)
        (turnovers if dur >= TURNOVER_MIN_S else micros).append(ev)
    # 睡眠期口径：入睡点之前的活动/数据混乱段不计入翻身统计
    cut = ts[onset] if onset is not None else settle
    turnovers_sl = [e for e in turnovers if e[0] >= cut]
    micros_sl = [e for e in micros if e[0] >= cut]
    pre_note = f"（另有入睡前活动段挂起 {len(turnovers)-len(turnovers_sl)} 次，未计入）" if len(turnovers) != len(turnovers_sl) else ""
    n_turn_h = len(turnovers_sl) / max((ts[-1] - cut) / 3600, 0.1)
    L += ["## 4. 体动与觉醒", "",
          f"- 大翻身（≥{TURNOVER_MIN_S//60}min 挂起）：**{len(turnovers_sl)} 次**（约 {n_turn_h:.1f} 次/小时，入睡点后计）{pre_note}",
          f"- 微体动（{MICRO_MIN_S}秒–{TURNOVER_MIN_S//60}分钟）：{len(micros_sl)} 次",
          f"- 连续性判定：{'好' if n_turn_h <= 0.8 else '一般' if n_turn_h <= 1.5 else '碎片化'}（>2min 挂起频率 {n_turn_h:.1f}/h）"]
    if has_metrics:
        typed_segs = [e for e in (turnovers_sl + micros_sl) if e[3] is not None]
        n_mo = sum(1 for e in typed_segs if e[3])
        L.append(f"- 挂起段分型（仅指标覆盖段）：体动型 {n_mo}/{len(typed_segs)}，其余为弱信号/信号消失型")
    for e in turnovers_sl:
        rec = f"  {fmt_ts(e[0])} 挂起 {fmt_dur(e[1])}"
        if not np.isnan(e[2]):
            rec += f"，恢复后 {e[2]:.1f} bpm"
        if e[3] is True:
            rec += "（体动型）"
        elif e[3] is False:
            rec += "（非体动，见 §5）"
        L.append(rec)
    L.append("")

    # ---------- 5. 呼吸暂停代理 ----------
    L += ["## 5. 呼吸暂停代理（探索性）", ""]
    if not has_metrics:
        L += ["本时段无逐窗功率指标（2026-10-06 起积累），无法分析。",
              "判据（今晚起生效）：呼吸带功率 ≤30% 滚动中位 且 raw_std 不升高（排除体动）且丢包 <20%，连续 ≥30s。", ""]
    else:
        ok = ~np.isnan(power)   # 仅在指标行上扫描
        p_base = rolling_median(np.nan_to_num(power, nan=np.nanmedian(power)))
        r_base = rolling_median(np.nan_to_num(raw_std, nan=np.nanmedian(raw_std)))
        cond = (ok
                & (power < APNEA_POWER_FRAC * p_base)
                & (raw_std < APNEA_RAW_MAX * r_base)
                & (np.nan_to_num(loss, nan=0) < APNEA_LOSS_MAX)
                & (p_base > 0))
        events = []
        for a, b in seg_runs(cond, ts):
            if (b - a) >= APNEA_MIN_WIN:
                dur = (b - a) * 10
                depth = float(np.nanmin(power[a:b] / p_base[a:b]))
                events.append((ts[a], dur, depth))
        # 合并 60s 内相邻事件
        merged = []
        for e in events:
            if merged and e[0] - (merged[-1][0] + merged[-1][1]) < 60:
                merged[-1] = (merged[-1][0], e[0] + e[1] - merged[-1][0], min(merged[-1][2], e[2]))
            else:
                merged.append(e)
        if merged:
            L.append(f"**候选事件 {len(merged)} 个**（合计 {sum(e[1] for e in merged)/60:.1f} 分钟，最长 {max(e[1] for e in merged)/60:.1f} 分钟）：")
            for e in merged[:15]:
                L.append(f"  - {fmt_ts(e[0])} 持续 {fmt_dur(e[1])}，功率降至基线 {e[2]*100:.0f}%")
            L += ["", "> 判据为功率塌缩+非体动，属**疑似低通气/长暂停**信号，非医学诊断；连续多夜复现才值得进一步检查。"]
        else:
            L.append("未检出候选事件（无 ≥40s 级的功率塌缩+非体动组合）。")
        L.append("")

    # ---------- 6. 图 ----------
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams["font.family"] = "Microsoft YaHei"
        plt.rcParams["axes.unicode_minus"] = False
        hh = (ts - ts[0]) / 3600
        n_ax = 3 if has_metrics else 2   # 无指标时不创建功率子图，避免底部空白
        fig, axes = plt.subplots(n_ax, 1, figsize=(15, 9 if n_ax == 2 else 10), sharex=True,
                                 gridspec_kw={"height_ratios": [2.4, 1] + ([1] if n_ax == 3 else [])})
        ax = axes[0]
        ax.scatter(hh[~v], np.where(np.isnan(bpm[~v]), 4, bpm[~v]).clip(4, 40), s=2, c="#ccc")
        ax.scatter(hh[v], bpm[v].clip(0, 40), s=3, c="#1a7f37")
        ax.axhline(ONSET_BPM, color="#2563eb", ls=":", lw=.8)
        if onset is not None:
            ax.axvline(hh[onset], color="#2563eb", ls="--", lw=.9)
            ax.text(hh[onset] + .03, 36, f"入睡 {fmt_ts(ts[onset])}", fontsize=10, color="#2563eb")
        for e in turnovers_sl:
            x = (e[0] - ts[0]) / 3600
            ax.axvline(x, color="#d97706", ls="--", lw=.9, alpha=.8)
            ax.text(x + .04, 25, f"翻身 {fmt_ts(e[0])}", fontsize=9, color="#b45309", rotation=90, va="top")
        ax.set_ylim(4, 40); ax.set_ylabel("呼吸率 (bpm)"); ax.grid(alpha=.3)
        ax.set_title(title, fontsize=12)
        bins = np.arange(0, hh[-1] + .5, .5)
        cov = [v[(hh >= b) & (hh < b + .5)].mean() * 100 if ((hh >= b) & (hh < b + .5)).any() else 0 for b in bins[:-1]]
        axes[1].bar(bins[:-1] + .25, cov, width=.45,
                    color=["#1a7f37" if c >= 50 else "#d97706" if c >= 25 else "#ccc" for c in cov])
        axes[1].set_ylabel("覆盖率%/30min"); axes[1].set_ylim(0, 100); axes[1].grid(alpha=.3)
        if has_metrics:
            pn = power / p_base
            axes[2].plot(hh, pn.clip(0, 3), lw=.7, color="#7c3aed")
            axes[2].axhline(APNEA_POWER_FRAC, color="#dc2626", ls=":", lw=.8)
            axes[2].text(hh[-1] - 2, APNEA_POWER_FRAC + .08, "暂停代理阈值 30%", fontsize=9, color="#dc2626")
            axes[2].set_ylabel("呼吸带功率/滚动中位"); axes[2].set_ylim(0, 3); axes[2].grid(alpha=.3)
        axes[-1].set_xlabel("时间")
        ticks = np.arange(0, hh[-1] + .01, 1)
        axes[-1].set_xticks(ticks)
        axes[-1].set_xticklabels([fmt_ts(ts[0] + t * 3600) for t in ticks],
                                 rotation=45, ha="right", fontsize=10, color="#333")
        axes[-1].tick_params(labelsize=10, colors="#333")
        plt.tight_layout()
        out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
        png = out_dir / f"sleep_report_{t0:%Y%m%d}.png"
        plt.savefig(png, dpi=110, bbox_inches="tight")
        L.append(f"![整夜图](sleep_report_{t0:%Y%m%d}.png)")
        L.append("")
        print("chart:", png)
    except Exception as e:  # 图失败不阻塞文字报告
        print("chart failed:", e)

    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / f"sleep_report_{t0:%Y%m%d}.md"
    md.write_text("\n".join(L), encoding="utf-8")
    print("report:", md)


if __name__ == "__main__":
    main()

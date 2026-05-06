"""E 鍵 — 匯出歷史 CSV / 圖表 / Slot 分析報告。

所有函數會在 EXPORT_DIR 下產出帶 timestamp 的檔名,呼叫端只需 await 取回 path。
"""
from __future__ import annotations

import asyncio
import csv
import os
from datetime import datetime

from bot.core.constants import EXPORT_DIR, HIGH_MULT_THRESHOLD
from bot.core.state import BotState
from bot.slot.analysis import (
    compute_drawdown,
    compute_slot_stats,
    format_symbol_display,
    is_noise_symbol,
)


def _export_filename(prefix: str, ext: str) -> str:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(EXPORT_DIR, f"{prefix}_{ts}.{ext}")


async def export_history_csv(state: BotState) -> str | None:
    history = state.history or []
    if not history:
        return None
    path = _export_filename("gambling", "csv")

    def _write() -> None:
        import json
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["時間", "下注", "下注前餘額", "下注後餘額", "變動", "結果", "中獎線路"])
            for r in history:
                lines_json = (json.dumps(r.get("lines", []), ensure_ascii=False)
                              if r.get("lines") else "")
                writer.writerow([
                    r.get("ts"), r.get("bet"), r.get("before"), r.get("after"),
                    r.get("change"), r.get("result"), lines_json,
                ])
    await asyncio.to_thread(_write)
    return path


async def export_history_chart(state: BotState) -> str | None:
    history = state.history or []
    if not history:
        return None

    def _draw() -> str | None:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            return None

        xs = list(range(1, len(history) + 1))
        balances = [r.get("after", 0) for r in history]
        nets = []
        cum = 0
        for r in history:
            cum += r.get("change", 0)
            nets.append(cum)

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
        ax1.plot(xs, balances, marker="o", linewidth=1.5, markersize=3, color="#1f77b4")
        ax1.set_ylabel("Balance")
        ax1.set_title(f"Discord Auto Bot - Gambling History ({len(history)} bets)")
        ax1.grid(True, alpha=0.3)

        colors = ["#2ca02c" if r.get("change", 0) >= 0 else "#d62728" for r in history]
        ax2.bar(xs, [r.get("change", 0) for r in history],
                color=colors, alpha=0.7, label="Per-bet change")
        ax2.plot(xs, nets, color="#ff7f0e", linewidth=2, label="Cumulative net")
        ax2.axhline(y=0, color="gray", linewidth=0.8)
        ax2.set_xlabel("Bet #")
        ax2.set_ylabel("Change")
        ax2.legend(loc="best")
        ax2.grid(True, alpha=0.3)

        path = _export_filename("gambling", "png")
        fig.tight_layout()
        fig.savefig(path, dpi=120)
        plt.close(fig)
        return path

    return await asyncio.to_thread(_draw)


async def export_slot_analysis(state: BotState) -> str | None:
    sa = state.slot_analysis or {}
    if sa.get("total_spins", 0) == 0:
        return None
    stats = compute_slot_stats(sa)
    path = _export_filename("slot_analysis", "txt")

    def _write() -> None:
        from bot.core.constants import PAYOUT_BUCKETS
        with open(path, "w", encoding="utf-8") as f:
            n = stats["total_spins"]
            f.write(f"Slot Analysis Report - {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            f.write(f"{'=' * 50}\n\n")
            f.write(f"Total spins:    {n}\n")
            f.write(f"Win rate:       {stats['win_rate']:.2%}\n")
            f.write(f"EV:             {stats['ev']:.4f}x (edge: {stats['edge']:+.2%})\n")
            f.write(f"Std dev:        {stats['std_dev']:.4f}\n")
            f.write(f"Variance:       {stats['variance']:.4f}\n")
            kf = stats["kelly_fraction"]
            f.write(f"Kelly fraction: {kf:.4f} (half: {kf / 2:.4f})\n\n")

            f.write("Payout Distribution\n")
            f.write(f"{'-' * 40}\n")
            dist = stats.get("payout_distribution", {})
            for bucket in PAYOUT_BUCKETS:
                count = int(dist.get(bucket, 0))
                pct = count / n * 100 if n else 0
                f.write(f"  {bucket:>6s}: {count:>5d} ({pct:5.1f}%)\n")
            high_mults = stats.get("high_mults", [])
            if high_mults:
                top = sorted(high_mults, reverse=True)
                f.write(f"  >={int(HIGH_MULT_THRESHOLD)}x actual multipliers ({len(top)}): "
                        + ", ".join(f"{m:.2f}x" for m in top) + "\n")

            si = stats.get("symbol_info", {})
            gp = stats.get("grid_symbol_prob", {})
            if si:
                f.write("\nSymbol Stats (from winning lines)\n")
                f.write(f"{'-' * 40}\n")
                f.write(f"  {'Symbol':<14s} {'Hits':>6s} {'AvgMult':>8s} {'TotalPay':>10s}\n")
                for sym, info in sorted(si.items(),
                                        key=lambda x: -x[1]["total_payout"]):
                    disp = format_symbol_display(sym)
                    f.write(f"  {disp:<14s} {info['win_appearances']:>6d} "
                            f"{info['avg_mult']:>8.2f}x {info['total_payout']:>10,}\n")

            if gp:
                f.write("\nGrid Symbol Probability\n")
                f.write(f"{'-' * 40}\n")
                hidden = 0
                for sym, prob in sorted(gp.items(), key=lambda x: -x[1]):
                    wins = si.get(sym, {}).get("win_appearances", 0)
                    if is_noise_symbol(sym, wins, prob):
                        hidden += 1
                        continue
                    disp = format_symbol_display(sym)
                    f.write(f"  {disp:<14s} {prob:>6.1%}\n")
                if hidden > 0:
                    f.write(f"  ({hidden} noise symbols hidden)\n")

            li = stats.get("line_info", {})
            if li:
                f.write("\nLine Stats\n")
                f.write(f"{'-' * 40}\n")
                f.write(f"  {'Line':<10s} {'Hits':>6s} {'Rate':>8s} {'TotalPay':>10s}\n")
                for ln, info in sorted(li.items(), key=lambda x: -x[1]["hits"]):
                    f.write(f"  {ln:<10s} {info['hits']:>6d} "
                            f"{info['hit_rate']:>7.1%} {info['total_payout']:>10,}\n")

            # Drawdown
            dd = compute_drawdown(state.history or [])
            f.write("\nDrawdown\n")
            f.write(f"{'-' * 40}\n")
            f.write(f"  Peak:              {dd['peak']:+,}\n")
            f.write(f"  Current net:       {dd['current_net']:+,}\n")
            f.write(f"  Max drawdown:      {dd['max_drawdown']:,}\n")
            f.write(f"  Current drawdown:  {dd['current_drawdown']:,}\n")

    await asyncio.to_thread(_write)
    return path

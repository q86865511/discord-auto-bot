"""S 鍵 — 顯示 Slot 分析報告。

呼叫端要先 live.stop() 再進這裡(會用 input() 等使用者按 Enter)。
"""
from __future__ import annotations

import os

from rich.console import Console
from rich.table import Table

from bot.core.constants import MIN_KELLY_SAMPLES
from bot.core.state import BotState
from bot.slot.analysis import (
    compute_drawdown,
    compute_hourly_breakdown,
    compute_slot_stats,
    format_symbol_display,
    is_noise_symbol,
)

console = Console()


def show_slot_analysis(state: BotState) -> None:
    """同步顯示分析報告;呼叫端應 live.stop() 後再呼叫。"""
    from bot.core.constants import PAYOUT_BUCKETS

    os.system("cls")
    sa = state.slot_analysis or {}
    stats = compute_slot_stats(sa)

    console.print()
    console.rule("[bold cyan]🎰  Slot Machine Analysis[/]")

    if stats.get("total_spins", 0) == 0:
        console.print("\n  [dim]尚無分析資料。開始賭博後會自動累積。[/dim]")
        _show_history_summary(state)
        input("\n  按 Enter 返回...")
        return

    n = stats["total_spins"]
    edge = stats["edge"]
    ec = "green" if edge >= 0 else "red"

    bt = Table(box=None, show_header=False, padding=(0, 2))
    bt.add_column(style="dim", no_wrap=True)
    bt.add_column()
    bt.add_row("總旋轉次數", f"{n:,}")
    bt.add_row("勝率",       f"{stats['win_rate']:.1%}")
    bt.add_row("期望值 (EV)", f"{stats['ev']:.4f}x  ([{ec}]邊際: {edge:+.2%}[/{ec}])")
    bt.add_row("標準差", f"{stats['std_dev']:.4f}")
    bt.add_row("變異數", f"{stats['variance']:.4f}")
    kf = stats["kelly_fraction"]
    if stats["sufficient_data"]:
        bt.add_row("Kelly f*", f"{kf:.4f}  (半 Kelly: {kf / 2:.4f})")
    else:
        valid_n = stats.get("valid_rr_count", n)
        bt.add_row("Kelly f*",
                   f"[dim]資料不足(需 {MIN_KELLY_SAMPLES} 筆,目前 {valid_n})[/dim]")
    console.print(bt)

    dist = stats.get("payout_distribution", {})
    high_mults = stats.get("high_mults", [])
    console.print()
    console.rule("[bold]📊 賠率分布[/]")
    dt = Table(show_header=True, header_style="bold cyan")
    dt.add_column("區間",     justify="right", no_wrap=True)
    dt.add_column("次數",     justify="right")
    dt.add_column("比例",     justify="right")
    dt.add_column("分布",     justify="left", no_wrap=True)
    dt.add_column("實際賠率", justify="left")
    for bucket in PAYOUT_BUCKETS:
        count = int(dist.get(bucket, 0))
        pct = count / n * 100 if n else 0
        bar = "█" * min(40, int(pct / 2))
        actual = ""
        if bucket == "以上" and count > 0 and high_mults:
            recent = sorted(high_mults, reverse=True)[:5]
            actual = ", ".join(f"{m:.1f}x" for m in recent)
            if len(high_mults) > len(recent):
                actual += f"  [dim]…+{len(high_mults) - len(recent)}[/dim]"
        dt.add_row(bucket, f"{count:,}", f"{pct:.1f}%", bar, actual)
    console.print(dt)

    si = stats.get("symbol_info", {})
    gp = stats.get("grid_symbol_prob", {})
    total_wagered = sa.get("total_wagered", 0) or 1
    if si or gp:
        console.print()
        console.rule("[bold]🎯 符號統計  (回收率 = 累計賠付 / 累計下注)[/]")
        st = Table(show_header=True, header_style="bold cyan")
        st.add_column("符號",     justify="left",  no_wrap=True)
        st.add_column("中獎次數", justify="right")
        st.add_column("平均倍率", justify="right")
        st.add_column("累計賠付", justify="right")
        st.add_column("回收率",   justify="right")
        if gp:
            st.add_column("格子機率", justify="right")

        all_symbols = set(si.keys()) | set(gp.keys())
        hidden_count = 0
        for sym in sorted(all_symbols,
                          key=lambda s: -(si.get(s, {}).get("total_payout", 0))):
            info = si.get(sym, {})
            wins = info.get("win_appearances", 0)
            prob = gp.get(sym, 0.0)
            if is_noise_symbol(sym, wins, prob):
                hidden_count += 1
                continue
            disp_sym = format_symbol_display(sym)
            if wins > 0:
                avg_mult = info.get("avg_mult", 0.0)
                total_pay = info.get("total_payout", 0)
                rec_rate = total_pay / total_wagered
                row = [disp_sym, f"{wins:,}", f"{avg_mult:.2f}x",
                       f"{total_pay:,}", f"{rec_rate:.1%}"]
            else:
                row = [disp_sym, "─", "─", "─", "─"]
            if gp:
                row.append(f"{prob:.1%}" if sym in gp else "─")
            st.add_row(*row)
        console.print(st)
        if hidden_count > 0:
            console.print(f"  [dim]({hidden_count} 個雜訊符號已隱藏)[/dim]")

    li = stats.get("line_info", {})
    if li:
        console.print()
        console.rule("[bold]📐 線路統計[/]")
        lt = Table(show_header=True, header_style="bold cyan")
        lt.add_column("線路",     justify="left",  no_wrap=True)
        lt.add_column("命中次數", justify="right")
        lt.add_column("命中率",   justify="right")
        lt.add_column("總賠付",   justify="right")
        for ln, info in sorted(li.items(), key=lambda x: -x[1]["hits"]):
            lt.add_row(ln, f"{info['hits']:,}",
                       f"{info['hit_rate']:.1%}",
                       f"{info['total_payout']:,}")
        console.print(lt)

    # 時段分析
    history = state.history or []
    if history:
        hourly = compute_hourly_breakdown(history)
        active = [h for h in hourly if h["bets"] > 0]
        if active:
            console.print()
            console.rule("[bold]🕐 時段分析(每小時)[/]")
            ht = Table(show_header=True, header_style="bold cyan")
            ht.add_column("時段", justify="right")
            ht.add_column("下注次數", justify="right")
            ht.add_column("勝率", justify="right")
            ht.add_column("平均賠率", justify="right")
            ht.add_column("總淨收", justify="right")
            ht.add_column("平均淨收", justify="right")

            best_change = max((h["total_change"] for h in active), default=0)
            worst_change = min((h["total_change"] for h in active), default=0)

            for h in sorted(active, key=lambda r: r["hour"]):
                tc = h["total_change"]
                tc_color = "green" if tc > 0 else ("red" if tc < 0 else "dim")
                hr_str = f"{h['hour']:02d}:00"
                if h["bets"] >= 10 and tc == best_change and tc > 0:
                    hr_str = f"[bold green]🏆 {hr_str}[/bold green]"
                elif h["bets"] >= 10 and tc == worst_change and tc < 0:
                    hr_str = f"[bold red]💀 {hr_str}[/bold red]"
                ht.add_row(
                    hr_str,
                    f"{h['bets']:,}",
                    f"{h['win_rate']:.1%}",
                    f"{h['avg_multiplier']:.3f}x",
                    f"[{tc_color}]{tc:+,}[/{tc_color}]",
                    f"{h['avg_change']:+,.0f}",
                )
            console.print(ht)
            console.print("  [dim]🏆/💀 標出 ≥10 把的時段裡最賺 / 最虧的[/dim]")

    # Drawdown
    dd = compute_drawdown(history)
    console.print()
    console.rule("[bold]📉 Drawdown(峰值跌幅)[/]")
    dd_t = Table(box=None, show_header=False, padding=(0, 2))
    dd_t.add_column(style="dim")
    dd_t.add_column()
    peak_color = "green" if dd["peak"] >= 0 else "red"
    cur_color  = "green" if dd["current_net"] >= 0 else "red"
    dd_t.add_row("歷史峰值",    f"[{peak_color}]{dd['peak']:+,}[/{peak_color}]")
    dd_t.add_row("當前累計淨收", f"[{cur_color}]{dd['current_net']:+,}[/{cur_color}]")
    dd_t.add_row("最大跌幅",     f"[red]{dd['max_drawdown']:,}[/red]")
    dd_t.add_row("當前距峰值",   f"[yellow]{dd['current_drawdown']:,}[/yellow]")
    console.print(dd_t)

    _show_history_summary(state)
    input("\n  按 Enter 返回...")


def _show_history_summary(state: BotState) -> None:
    history = state.history or []
    if not history:
        return
    cum_net = sum(r.get("change", 0) for r in history)
    last_balance = history[-1].get("after", 0)
    win_count = sum(1 for r in history if r.get("change", 0) > 0)
    lose_count = sum(1 for r in history if r.get("change", 0) < 0)

    console.print()
    console.rule("[bold]📉 賭博紀錄[/]")
    color = "green" if cum_net >= 0 else "red"
    console.print(
        f"  下注次數: [bold]{len(history)}[/]  "
        f"勝/敗: [green]{win_count}[/]/[red]{lose_count}[/]  "
        f"累計淨收: [{color}]{cum_net:+,}[/]  "
        f"目前餘額: [bold]{last_balance:,}[/]"
    )

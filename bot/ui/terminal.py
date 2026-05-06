"""終端 Rich UI:layout、ui_loop、export 鍵盤動作。"""
from __future__ import annotations

import asyncio
import csv
import logging
import os
import threading
import time
import webbrowser
from datetime import datetime
from typing import TYPE_CHECKING, Awaitable, Callable

import msvcrt
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bot.core.constants import (
    DEFAULT_INTERVAL_MAX,
    DEFAULT_INTERVAL_MIN,
    DEFAULT_NOTIFY_USER_ID,
    DEFAULT_TRANSFER_INTERVAL_MIN,
    EXPORT_DIR,
    HIGH_MULT_THRESHOLD,
    MIN_KELLY_SAMPLES,
)
from bot.core.state import BotState
from bot.slot.analysis import (
    compute_drawdown,
    compute_hourly_breakdown,
    compute_slot_stats,
    format_symbol_display,
    is_noise_symbol,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

    from bot.core.config import BotConfig

log = logging.getLogger(__name__)
console = Console()


# ── 鍵盤監聽(背景 thread) ───────────────────────────────────────────
def start_kb_listener(state: BotState) -> None:
    def _listen() -> None:
        while not state.quit:
            try:
                if msvcrt.kbhit():
                    raw = msvcrt.getch()
                    try:
                        key = raw.decode("utf-8").lower()
                        state.push_key(key)
                    except UnicodeDecodeError:
                        pass
            except OSError as e:
                log.debug("kb listener OSError: %s", e)
            time.sleep(0.05)
    threading.Thread(target=_listen, daemon=True, name="kb-listener").start()


# ── Layout ────────────────────────────────────────────────────────────
def fmt_remaining(ts: float | None) -> str:
    if ts is None:
        return "─"
    remaining = ts - time.time()
    if remaining <= 0:
        return "[green]即將執行[/green]"
    h, r = divmod(int(remaining), 3600)
    m, s = divmod(r, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def build_layout(state: BotState, config: "BotConfig") -> Layout:
    gcfg     = config.gambling
    balance  = state.balance
    start    = state.start_balance
    total    = state.total_bets
    net      = state.net_change
    win_rate = state.wins / total * 100 if total > 0 else 0

    if state.paused:
        status_text  = "已暫停"
        status_color = "yellow"
    else:
        status_text  = state.status
        status_color = "green" if status_text == "運行中" else "yellow"

    header = Panel(
        Text.from_markup(
            f"🤖 Discord Auto Bot  |  [{status_color}]{status_text}[/{status_color}]",
            justify="center",
        ),
        style="bold cyan", height=3,
    )

    bal_str = f"[bold]{balance:,}[/bold]" if isinstance(balance, int) else "[dim]讀取中...[/dim]"
    if isinstance(balance, int) and isinstance(start, int):
        diff = balance - start
        dc = "green" if diff >= 0 else "red"
        diff_str = f"[{dc}]{'+' if diff > 0 else ''}{diff:,}[/{dc}]"
    else:
        diff_str = "─"
    nc = "green" if net >= 0 else "red"
    net_str = f"[{nc}]{'+' if net > 0 else ''}{net:,}[/{nc}]"
    start_str = f"{start:,}" if isinstance(start, int) else "─"
    cur_bet = state.current_bet

    goal = int(gcfg.goal or 0)
    if goal > 0 and isinstance(balance, int):
        pct = min(100.0, balance / goal * 100)
        gc = "green" if balance >= goal else "yellow"
        goal_str = f"[{gc}]{balance:,} / {goal:,} ({pct:.1f}%)[/{gc}]"
    elif goal > 0:
        goal_str = f"─ / {goal:,}"
    else:
        goal_str = "[dim]未設定[/dim]"

    loss_floor = int(gcfg.loss_floor or 0)
    if loss_floor > 0 and isinstance(balance, int):
        if balance <= loss_floor:
            loss_str = f"[red]{balance:,} ≤ {loss_floor:,}(已觸發)[/red]"
        else:
            buffer = balance - loss_floor
            loss_str = f"[green]{balance:,} > {loss_floor:,}  (+{buffer:,} 緩衝)[/green]"
    elif loss_floor > 0:
        loss_str = f"─ > {loss_floor:,}"
    else:
        loss_str = "[dim]未設定[/dim]"

    t1 = Table(box=None, show_header=False, padding=(0, 2), expand=True)
    t1.add_column(style="dim", width=14)
    t1.add_column()
    t1.add_row("💰 目前餘額",  bal_str)
    t1.add_row("📌 起始餘額",  start_str)
    t1.add_row("📈 本次盈虧",  diff_str)
    t1.add_row("🏁 目標進度",  goal_str)
    t1.add_row("⛔ 停損狀態",  loss_str)
    t1.add_row("", "")
    t1.add_row("🎲 總下注",    str(total))
    t1.add_row("✅ 獲勝",      f"[green]{state.wins}[/green]")
    t1.add_row("❌ 失敗",      f"[red]{state.losses}[/red]")
    t1.add_row("📊 勝率",      f"{win_rate:.1f}%")

    cs = state.current_streak
    if cs > 0:
        streak_str = (f"[green]{cs} 連勝[/green]  "
                      f"(歷史最高 {state.max_win_streak}勝/{state.max_loss_streak}敗)")
    elif cs < 0:
        streak_str = (f"[red]{abs(cs)} 連敗[/red]  "
                      f"(歷史最高 {state.max_win_streak}勝/{state.max_loss_streak}敗)")
    else:
        streak_str = (f"[dim]─[/dim]  "
                      f"(歷史最高 {state.max_win_streak}勝/{state.max_loss_streak}敗)")
    t1.add_row("🔥 連勝紀錄",   streak_str)

    sess_start = state.session_start_ts
    pp_str = "─"
    if sess_start:
        hrs = max(1/60, (time.time() - sess_start) / 3600)
        pph = state.net_change / hrs
        pp_color = "green" if pph >= 0 else "red"
        pp_str = f"[{pp_color}]{pph:+,.0f}[/{pp_color}] / 小時  ({hrs:.1f}h)"
    t1.add_row("💴 平均時薪",   pp_str)

    t1.add_row("💵 賭博淨收",  net_str)

    sa = state.slot_analysis or {}
    sa_n = sa.get("total_spins", 0)
    sa_stats = compute_slot_stats(sa) if sa_n > 0 else None
    if sa_n > 0 and sa_stats:
        ev_c = "green" if sa_stats["edge"] >= 0 else "red"
        ev_str = (f"[{ev_c}]{sa_stats['ev']:.3f}x ({sa_stats['edge']:+.1%})[/{ev_c}]"
                  f"  n={sa_n}")
    else:
        ev_str = "[dim]資料不足[/dim]"
    t1.add_row("📈 EV/期望值", ev_str)

    t1.add_row("", "")
    t1.add_row("🎯 當前下注",
               f"[yellow]{cur_bet:,}[/yellow]" if cur_bet else "─")
    stats_panel = Panel(t1, title="[bold]📊 統計[/bold]", border_style="blue")

    # ── 設定面板 ─────────────────────────────────────────────────────
    strategy    = gcfg.strategy
    frac_pct    = f"{gcfg.bet_fraction * 100:.1f}%"
    max_bet     = gcfg.max_bet
    max_bet_str = f"{max_bet:,}" if max_bet > 0 else "自動"
    strat_label = f"[cyan]{strategy}[/cyan]"
    if strategy == "auto":
        strat_label += f" ({frac_pct})"
    elif strategy == "kelly":
        if sa_n >= MIN_KELLY_SAMPLES and sa_stats:
            kf = sa_stats["kelly_fraction"]
            strat_label += f" (f*={kf:.3f})"
        else:
            strat_label += f" ({sa_n}/{MIN_KELLY_SAMPLES})"

    interval_str = f"{gcfg.interval_min}-{gcfg.interval_max}s"

    notify_uid = gcfg.notify_user_id or DEFAULT_NOTIFY_USER_ID
    notify_str = f"…{str(notify_uid)[-6:]}" if notify_uid else "[dim]未設定[/dim]"

    t2 = Table(box=None, show_header=False, padding=(0, 2), expand=True)
    t2.add_column(style="dim", width=14)
    t2.add_column()
    t2.add_row("⚙️ 策略",    strat_label)
    t2.add_row("🏦 保底門檻", f"{gcfg.threshold:,}")
    t2.add_row("⬇️ 最小下注", f"{gcfg.min_bet:,}")
    t2.add_row("⬆️ 最大下注", max_bet_str)
    t2.add_row("⏱️ 下注間距", interval_str)
    t2.add_row("🏁 目標餘額", f"{goal:,}" if goal > 0 else "[dim]未設定[/dim]")
    t2.add_row("📣 通知對象", notify_str)
    t2.add_row("🎮 賭博",     "[green]啟用[/green]" if gcfg.enabled else "[red]停用[/red]")
    t2.add_row("", "")
    t2.add_row("⏰ /hourly",  fmt_remaining(state.hourly_next))
    t2.add_row("📅 /daily",   fmt_remaining(state.daily_next))

    neko_st = state.neko_status
    neko_dl = state.neko_deadline_ts
    if neko_st == "dispatching" and neko_dl is not None:
        neko_str = f"[yellow]派遣中 {fmt_remaining(neko_dl)}[/yellow]"
    elif neko_st == "dispatching":
        neko_str = "[yellow]派遣中[/yellow]"
    elif neko_st == "not_dispatching":
        neko_str = "[green]待領取/閒置[/green]"
    else:
        neko_str = "[dim]─[/dim]"
    t2.add_row("🐱 貓娘",     neko_str)

    tcfg = config.transfer
    if tcfg.enabled:
        tr_amt = int(tcfg.amount or 0)
        tr_target = tcfg.target or "—"
        tr_int = tcfg.interval_min
        transfer_str = f"[green]{tr_target} {tr_amt:,}/次 ({tr_int}m)[/green]"
    else:
        transfer_str = "[dim]停用[/dim]"
    t2.add_row("💸 自動轉帳",  transfer_str)

    dcfg = config.dashboard
    if dcfg.enabled:
        from bot.web.url_helpers import dashboard_lan_url
        lan_url = dashboard_lan_url(config).rstrip("/")
        if dcfg.password:
            dash_str = f"[cyan]{lan_url}/[/cyan]  [green]🔒[/green]"
        else:
            dash_str = f"[cyan]{lan_url}/[/cyan]  [yellow]⚠ 無密碼[/yellow]"
    else:
        dash_str = "[dim]停用[/dim]"
    t2.add_row("🌐 Dashboard", dash_str)

    cfg_panel = Panel(t2, title="[bold]⚙️ 設定[/bold]  [dim]C:修改系統設定[/dim]",
                      border_style="green")

    # 日誌
    lines    = list(state.log_lines)[-10:]
    log_text = "\n".join(lines) if lines else "[dim]尚無日誌[/dim]"
    log_panel = Panel(log_text, title="[bold]📋 日誌[/bold]", border_style="dim", height=13)

    # Footer
    pause_label = (
        "[yellow]P 恢復系統[/yellow]"
        if state.paused
        else "[bold]P[/bold] 暫停系統"
    )
    footer = Panel(
        f"[dim][bold]Q[/bold] 退出  [bold]C[/bold] 修改系統設定  "
        f"{pause_label}  "
        f"[bold]E[/bold] 匯出分析結果  "
        f"[bold]S[/bold] 分析賭博機率  "
        f"[bold]W[/bold] 開啟 Dashboard  "
        f"[bold]K[/bold] 開啟 QR 圖  "
        f"[bold]F[/bold] 重啟程式[/dim]",
        style="dim", height=3,
    )

    layout = Layout()
    layout.split_column(
        Layout(header,    name="header", size=3),
        Layout(name="body"),
        Layout(log_panel, name="logs",   size=13),
        Layout(footer,    name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(stats_panel, name="stats"),
        Layout(cfg_panel,   name="cfg"),
    )
    return layout


# ── Export ────────────────────────────────────────────────────────────
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


# ── Slot 分析顯示(S 鍵) ────────────────────────────────────────────
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


# ── UI Loop ───────────────────────────────────────────────────────────
async def ui_loop(
    state: BotState,
    config_provider: Callable[[], "BotConfig"],
    on_menu: Callable[[], Awaitable[None]],
    on_qr_open: Callable[[], Awaitable[None]],
    on_export: Callable[[], Awaitable[None]],
) -> None:
    with Live(
        build_layout(state, config_provider()),
        console=console, refresh_per_second=2, screen=True,
    ) as live:
        while not state.quit:
            key = state.pop_key()
            if key:
                if key == "q":
                    async with state.lock:
                        state.quit = True
                    break
                elif key == "c":
                    live.stop()
                    try:
                        await on_menu()
                    finally:
                        live.start()
                elif key == "p":
                    async with state.lock:
                        state.paused = not state.paused
                    state.queue_log("已暫停所有功能(再按 P 恢復)"
                                    if state.paused else "已恢復運行")
                elif key == "e":
                    await on_export()
                elif key == "s":
                    live.stop()
                    try:
                        show_slot_analysis(state)
                    finally:
                        live.start()
                elif key == "f":
                    state.queue_log("已請求重啟,正在收尾...")
                    async with state.lock:
                        state.reboot = True
                        state.quit = True
                    break
                elif key == "w":
                    from bot.web.url_helpers import dashboard_local_url
                    url = dashboard_local_url(config_provider())
                    try:
                        webbrowser.open(url)
                        state.queue_log(f"🌐 已在瀏覽器打開 {url}")
                    except Exception as e:    # noqa: BLE001
                        state.queue_log(f"⚠ 開啟瀏覽器失敗: {e}")
                elif key == "k":
                    await on_qr_open()

            live.update(build_layout(state, config_provider()))
            await asyncio.sleep(0.5)

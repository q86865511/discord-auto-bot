"""Rich Live UI layout 組裝。

由 ui_loop 每 ~0.5s 重建一次,把當前 state + config 渲染成 Rich Layout。
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from bot.core.constants import DEFAULT_NOTIFY_USER_ID, MIN_KELLY_SAMPLES
from bot.core.state import BotState
from bot.slot.analysis import compute_slot_stats

if TYPE_CHECKING:
    from bot.core.config import BotConfig


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


def build_layout(state: BotState, config: BotConfig) -> Layout:
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

    # ── 版本檢查 ────────────────────────────────────────────────────
    ucfg = config.updater
    if not ucfg.auto_check:
        upd_str = "[dim]停用[/dim]"
    elif state.update_available:
        local_short  = (state.local_commit  or "")[:7]
        remote_short = (state.remote_commit or "")[:7]
        if ucfg.auto_update:
            upd_str = (f"[bold yellow]🔔 {local_short}→{remote_short} "
                       f"自動更新中...[/bold yellow]")
        else:
            upd_str = f"[yellow]🔔 新版可用 {local_short}→{remote_short}[/yellow]"
    elif state.last_update_check:
        upd_str = "[dim]✓ 已是最新版[/dim]"
    else:
        upd_str = "[dim]檢查中...[/dim]"
    t2.add_row("🔄 版本檢查", upd_str)

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

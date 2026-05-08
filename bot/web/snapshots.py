"""Dashboard 用的 snapshot builders。

從 dashboard.py 拆出來,讓 handler 那邊只剩 routing。

公開符號:
    read_log_tail(path, max_lines, max_bytes) -> list[str]
    build_state_snapshot(state, config) -> dict
    build_analysis_snapshot(state) -> dict
    run_in_main_loop(main_loop, state, builder, *args, timeout) -> Any
        在 main asyncio loop 內(state.lock 保護下)呼叫 builder。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import TYPE_CHECKING

from bot.core.constants import (
    LOG_FILE_PATH,
    PAYOUT_BUCKETS,
)
from bot.core.log_filter import redact_text

if TYPE_CHECKING:
    import asyncio

    from bot.core.config import BotConfig
    from bot.core.state import BotState

log = logging.getLogger("dashboard")


def read_log_tail(path: str = LOG_FILE_PATH, max_lines: int = 200,
                  max_bytes: int = 256 * 1024) -> list[str]:
    """讀最後 N 行,redact 敏感字。"""
    try:
        if not os.path.exists(path):
            return []
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()[-max_lines:]
        return [redact_text(line) for line in lines]
    except OSError:
        return []


def build_state_snapshot(state: BotState, config: BotConfig) -> dict:
    """組 /api/state 快照(plain-text 欄位,前端組裝 HTML)。"""
    gcfg = config.gambling
    bal = state.balance
    start = state.start_balance
    goal = int(gcfg.goal or 0)

    if goal > 0 and isinstance(bal, int):
        pct = min(100.0, bal / goal * 100)
        goal_str = f"{bal:,} / {goal:,} ({pct:.1f}%)"
    elif goal > 0:
        goal_str = f"─ / {goal:,}"
    else:
        goal_str = "未設定"

    floor = int(gcfg.loss_floor or 0)
    if floor > 0 and isinstance(bal, int):
        if bal <= floor:
            loss_str = f"⚠ {bal:,} ≤ {floor:,}"
        else:
            loss_str = f"{bal:,} > {floor:,} (緩衝 +{bal - floor:,})"
    elif floor > 0:
        loss_str = f"─ > {floor:,}"
    else:
        loss_str = "未設定"

    ev_str = "─"
    kelly_str = "─"
    sa = state.slot_analysis or {}
    n = sa.get("total_spins", 0)
    if n > 0:
        try:
            from bot.slot.analysis import compute_slot_stats, format_kelly_display
            stats = compute_slot_stats(sa)
            edge_pct = stats["edge"] * 100
            ev_str = (f"{stats['ev']:.3f}x ({'+' if edge_pct >= 0 else ''}"
                      f"{edge_pct:.2f}%) n={n}")
            kelly_str = format_kelly_display(stats)
        except Exception:    # noqa: BLE001
            log.exception("compute_slot_stats 失敗")

    neko_st = state.neko_status
    neko_dl = state.neko_deadline_ts
    if neko_st == "dispatching":
        if neko_dl:
            r = max(0, int(neko_dl - time.time()))
            h, rem = divmod(r, 3600)
            m = rem // 60
            neko_str = f"派遣中 {h}h{m:02d}m" if h else f"派遣中 {m}m"
        else:
            neko_str = "派遣中"
    elif neko_st == "not_dispatching":
        neko_str = "閒置/待領取"
    else:
        neko_str = "─"

    history = state.history or []
    history_last_15 = history[-15:]
    history_recent = [{"change": r.get("change", 0)} for r in history[-100:]]

    cs = state.current_streak
    max_w = state.max_win_streak
    max_l = state.max_loss_streak
    if cs > 0:
        streak_str = f"🔥 {cs} 連勝 (最高 {max_w}勝/{max_l}敗)"
    elif cs < 0:
        streak_str = f"💀 {abs(cs)} 連敗 (最高 {max_w}勝/{max_l}敗)"
    else:
        streak_str = f"─ (最高 {max_w}勝/{max_l}敗)"

    sess_start = state.session_start_ts
    pph_str = "─"
    if sess_start:
        hrs = max(1/60, (time.time() - sess_start) / 3600)
        pph = state.net_change / hrs
        pph_str = f"{'+' if pph >= 0 else ''}{int(pph):,} / 小時 ({hrs:.1f}h)"

    return {
        "ts":            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status":        state.status,
        "paused":        state.paused,
        "balance":       bal,
        "start_balance": start,
        "net_change":    state.net_change,
        "current_bet":   state.current_bet,
        "total_bets":    state.total_bets,
        "wins":          state.wins,
        "losses":        state.losses,
        "goal_str":      goal_str,
        "loss_str":      loss_str,
        "ev_str":        ev_str,
        "kelly_str":     kelly_str,
        "streak_str":    streak_str,
        "pph_str":       pph_str,
        "hourly_next":   state.hourly_next,
        "daily_next":    state.daily_next,
        "neko_str":      neko_str,
        "events":        dict(state.events.__dict__),
        "dead_notified": state.dead_notified,
        "history_last_15": history_last_15,
        "history_recent":  history_recent,
    }


def build_analysis_snapshot(state: BotState) -> dict:
    sa = state.slot_analysis or {}
    n = sa.get("total_spins", 0)
    if n == 0:
        return {"has_data": False, "total_spins": 0}

    from bot.slot.analysis import (
        compute_drawdown,
        compute_slot_stats,
        format_kelly_display,
        format_symbol_display,
        is_noise_symbol,
    )

    stats = compute_slot_stats(sa)
    total_wagered = sa.get("total_wagered", 0) or 1

    high_mults = stats.get("high_mults", [])
    dist_rows = []
    for bucket in PAYOUT_BUCKETS:
        count = int(stats["payout_distribution"].get(bucket, 0))
        pct = count / n * 100 if n else 0
        actual = ""
        if bucket == "以上" and count > 0 and high_mults:
            recent = sorted(high_mults, reverse=True)[:5]
            actual = ", ".join(f"{m:.1f}x" for m in recent)
            if len(high_mults) > len(recent):
                actual += f" …+{len(high_mults) - len(recent)}"
        dist_rows.append({
            "bucket": bucket, "count": count, "pct": pct, "actual": actual,
        })

    si = stats.get("symbol_info", {}) or {}
    gp = stats.get("grid_symbol_prob", {}) or {}
    all_syms = set(si.keys()) | set(gp.keys())
    sym_rows = []
    for sym in sorted(all_syms,
                      key=lambda s: -(si.get(s, {}).get("total_payout", 0))):
        info = si.get(sym, {})
        wins = info.get("win_appearances", 0)
        prob = gp.get(sym)
        is_noise = is_noise_symbol(sym, wins, prob or 0)
        sym_rows.append({
            "symbol":        sym,
            "display":       format_symbol_display(sym),
            "wins":          wins,
            "avg_mult":      info.get("avg_mult", 0.0),
            "total_payout":  info.get("total_payout", 0),
            "recover_rate":  info.get("total_payout", 0) / total_wagered,
            "grid_prob":     prob,
            "hidden":        is_noise,
        })

    li = stats.get("line_info", {}) or {}
    line_rows = sorted(
        [{"line_name": ln, **info} for ln, info in li.items()],
        key=lambda r: -r["hits"],
    )

    kelly_str = format_kelly_display(stats)

    drawdown = compute_drawdown(state.history or [])

    return {
        "has_data":       True,
        "total_spins":    stats["total_spins"],
        "win_rate":       stats["win_rate"],
        "ev":             stats["ev"],
        "edge":           stats["edge"],
        "std_dev":        stats["std_dev"],
        "variance":       stats["variance"],
        "kelly_str":      kelly_str,
        "payout_distribution": dist_rows,
        "symbols":        sym_rows,
        "lines":          line_rows,
        "drawdown":       drawdown,
    }


def build_stocks_snapshot(state: BotState, config: BotConfig) -> dict:
    """股票快照 — 最新價格 / 持股 / 買賣訊號。"""
    snap = state.stock_last_snapshot or {}
    scfg = config.stock
    return {
        "enabled":        scfg.enabled,
        "last_poll_ts":   state.stock_last_poll_ts,
        "ts":             snap.get("ts"),
        "prices":         snap.get("prices", {}),
        "holdings":       snap.get("holdings", {}),
        "signals":        snap.get("signals", []),
        "config": {
            "poll_interval_min":      scfg.poll_interval_min,
            "ma_short":               scfg.ma_short,
            "ma_long":                scfg.ma_long,
            "take_profit_pct":        scfg.take_profit_pct,
            "stop_loss_pct":          scfg.stop_loss_pct,
            "signal_score_threshold": scfg.signal_score_threshold,
            "stock_command":          scfg.stock_command,
            "portfolio_command":      scfg.portfolio_command,
            "tracked_symbols":        list(scfg.tracked_symbols),
        },
    }


def build_strategies_snapshot(state: BotState, config: BotConfig) -> dict:
    """Backtest 三個進階策略 + runtime 統計。

    回傳給 /api/strategies。前端用來在 dashboard 顯示「策略比較」表格。
    """
    from bot.slot.strategies import backtest_all, hourly_stats

    history = state.history or []
    if not history:
        return {
            "has_data": False,
            "config": _strategy_config_dict(config.gambling),
            "runtime": _strategy_runtime_dict(state),
            "results": {},
            "hourly": [],
        }

    scfg = _strategy_config_dict(config.gambling)
    # Backtest 永遠顯示所有 4 個情境(就算 disabled 也算給使用者看效果)
    # 用「忽略 enabled flag」的方式讓比較更直觀
    scfg_force = {**scfg,
                  "hourly_filter_enabled": True,
                  "rolling_enabled": True,
                  "trailing_stop_enabled": True}
    results = backtest_all(history, scfg_force)

    # Hourly stats(供前端畫小時 EV 分布圖用)
    h_stats = hourly_stats(history)
    hourly_rows = []
    for h in range(24):
        s = h_stats.get(h, {})
        hourly_rows.append({
            "hour":     h,
            "bets":     s.get("bets", 0),
            "win_rate": s.get("win_rate", 0.0),
            "ev":       s.get("ev", 0.0),
        })

    return {
        "has_data": True,
        "n_history": len(history),
        "config":   scfg,
        "runtime":  _strategy_runtime_dict(state),
        "results":  results,
        "hourly":   hourly_rows,
    }


def _strategy_config_dict(g) -> dict:
    # Backtest 內部用 cooldown_bets;從 cooldown_min 估算(~30s/bet → 2 bets/min)
    cooldown_bets_est = int(g.trailing_stop_cooldown_min * 2)
    return {
        "hourly_filter_enabled":  g.hourly_filter_enabled,
        "hourly_min_bets":        g.hourly_min_bets,
        "hourly_min_winrate":     g.hourly_min_winrate,
        "hourly_min_ev":          g.hourly_min_ev,
        "rolling_enabled":        g.rolling_enabled,
        "rolling_window_size":    g.rolling_window_size,
        "rolling_low_ev":         g.rolling_low_ev,
        "rolling_high_ev":        g.rolling_high_ev,
        "rolling_low_mult":       g.rolling_low_mult,
        "rolling_high_mult":      g.rolling_high_mult,
        "trailing_stop_enabled":      g.trailing_stop_enabled,
        "trailing_stop_pct":          g.trailing_stop_pct,
        "trailing_stop_cooldown_min": g.trailing_stop_cooldown_min,
        "trailing_stop_cooldown_bets": cooldown_bets_est,    # 給 backtest 用
    }


def _strategy_runtime_dict(state: BotState) -> dict:
    import time as _time
    cd_until = state.trailing_cooldown_until_ts
    cd_remain_sec = max(0.0, cd_until - _time.time()) if cd_until else 0.0
    return {
        "skipped_hourly":     state.strategy_skipped_hourly,
        "skipped_trailing":   state.strategy_skipped_trailing,
        "trailing_triggers":  state.strategy_trailing_triggers,
        "recent_ev_mult":     state.strategy_recent_ev_mult,
        "trailing_cooldown_remaining_sec": int(cd_remain_sec),
        "trailing_baseline_idx":           state.trailing_baseline_idx,
    }


def run_in_main_loop(
    main_loop: asyncio.AbstractEventLoop | None,
    state: BotState,
    builder,
    *args,
    timeout: float = 2.0,
):
    """在 main asyncio loop 內(state.lock 保護下)呼叫 builder(...) 組快照。

    給 dashboard thread 用 — 避免直接讀 main loop 正在改的 state。

    builder 是 sync function。我們在 main loop 內 wrap 一個 coroutine,
    取得 state.lock 後再呼叫 builder — 這樣 builder 看到的是「同一瞬間」
    的 state 而不是更新一半的中間狀態。

    若 main_loop=None(舊呼叫者沒提供),fallback 直接呼叫 builder 不加 lock。
    """
    if main_loop is None:
        return builder(*args)
    import asyncio as _aio

    async def _wrap():
        async with state.lock:
            return builder(*args)
    fut = _aio.run_coroutine_threadsafe(_wrap(), main_loop)
    try:
        return fut.result(timeout=timeout)
    except (TimeoutError, _aio.TimeoutError):
        log.warning("snapshot 在 %.1fs 內未返回 — 改 fallback 直讀 state", timeout)
        return builder(*args)

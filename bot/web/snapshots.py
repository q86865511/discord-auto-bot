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
    MIN_KELLY_SAMPLES,
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
            from bot.slot.analysis import compute_slot_stats
            stats = compute_slot_stats(sa)
            edge_pct = stats["edge"] * 100
            ev_str = (f"{stats['ev']:.3f}x ({'+' if edge_pct >= 0 else ''}"
                      f"{edge_pct:.2f}%) n={n}")
            if stats.get("sufficient_data") and stats.get("kelly_fraction", 0) > 0:
                kf = stats["kelly_fraction"]
                kelly_str = f"{kf:.4f} (½={kf/2:.4f})"
            else:
                kelly_str = f"資料不足 (需 {MIN_KELLY_SAMPLES})"
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

    if stats.get("sufficient_data") and stats.get("kelly_fraction", 0) > 0:
        kf = stats["kelly_fraction"]
        kelly_str = f"{kf:.4f} (½={kf/2:.4f})"
    else:
        valid_n = stats.get("valid_rr_count", n)
        kelly_str = f"資料不足 (需 {MIN_KELLY_SAMPLES},目前 {valid_n})"

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

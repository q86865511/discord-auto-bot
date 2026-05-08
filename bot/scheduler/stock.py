"""股票監視 + 建議 loop。

工作流程(每 poll_interval_min 分鐘一次):
1. 送 /portfolio → 一次拿所有持股 + 現價,寫進 stock_prices + stock_holdings
2. 對 tracked_symbols(使用者觀察名單,沒持有的)各送一次 /stock symbol:X
   抓現價,寫 stock_prices
3. 對所有有歷史的 symbol 做 buy/sell 分析
4. 強訊號(score ≥ threshold)寫進 state.queue_log

Phase 1-2 純建議,不自動下單。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

from bot.core.state import BotState, interruptible_sleep
from bot.discord.client import query_stock_text
from bot.stock.analysis import analyze_symbol, group_by_symbol
from bot.stock.parser import parse_portfolio, parse_stock_detail

if TYPE_CHECKING:
    from playwright.async_api import Page

    from bot.core.config import BotConfig
    from bot.core.db import Database

log = logging.getLogger(__name__)


async def stock_loop(
    page: "Page",
    state: BotState,
    config_provider: Callable[[], "BotConfig"],
    on_config_save: Callable[["BotConfig"], Awaitable[None]],   # noqa: ARG001
    db: "Database",
) -> None:
    # 啟動延遲(讓其他 loop 先穩定 + 避免跟 /balance 撞在一起)
    await interruptible_sleep(state, 60)

    while not state.quit:
        scfg = config_provider().stock
        if not scfg.enabled:
            await interruptible_sleep(state, 60)
            continue

        try:
            await _poll_once(page, state, scfg, db)
        except Exception:    # noqa: BLE001
            log.exception("stock loop 例外")

        sleep_sec = max(60, int(float(scfg.poll_interval_min) * 60))
        await interruptible_sleep(state, sleep_sec)


async def _poll_once(page, state: BotState, scfg, db) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_prices: dict[str, float] = {}

    # ── 1. /portfolio:抓所有持股 + 現價 ───────────────────────────
    log.info("stock: 查 portfolio (%s)", scfg.portfolio_command)
    pf_text = await query_stock_text(page, command=scfg.portfolio_command)
    holdings: dict[str, dict] = {}
    if pf_text:
        if scfg.log_raw_text:
            log.info("portfolio raw text:\n%s", pf_text[:2000])
        holdings = parse_portfolio(pf_text)
        log.info("stock: portfolio 解析到 %d 支持股 — %s", len(holdings),
                 ", ".join(f"{s}×{int(h['shares'])}@{h['current_price']:.2f}"
                           for s, h in list(holdings.items())[:6]) or "(無)")
        # 現價寫進 prices 累計
        for sym, info in holdings.items():
            if info.get("current_price", 0) > 0:
                all_prices[sym] = info["current_price"]
        # 持股快照 — 全部清掉重寫,確保已賣的不殘留
        await db.clear_stock_holdings()
        for sym, info in holdings.items():
            await db.upsert_stock_holding(
                sym, info["shares"], info["avg_cost"], ts,
            )
    else:
        log.warning("stock: %s 無回應", scfg.portfolio_command)

    # ── 2. tracked_symbols:對沒持有的觀察清單,各送一次 /stock ────
    held_set = set(holdings.keys())
    tracked = [
        s.upper().strip() for s in (scfg.tracked_symbols or [])
        if s and s.strip()
    ]
    to_query = [s for s in tracked if s not in held_set]
    for sym in to_query:
        if state.quit:
            return
        try:
            stock_text = await query_stock_text(
                page, command=scfg.stock_command, param=f"symbol: {sym}",
            )
            if not stock_text:
                log.warning("stock: %s symbol:%s 無回應", scfg.stock_command, sym)
                continue
            if scfg.log_raw_text:
                log.info("stock %s raw text:\n%s", sym, stock_text[:1500])
            detail = parse_stock_detail(stock_text, expected_symbol=sym)
            if detail and detail.get("current_price", 0) > 0:
                all_prices[detail["symbol"]] = detail["current_price"]
                log.info("stock: %s @ %.2f", detail["symbol"], detail["current_price"])
            else:
                log.warning("stock: 無法解析 %s 的 detail", sym)
        except Exception:    # noqa: BLE001
            log.exception("stock: 查詢 %s 失敗", sym)
        # 各支股之間小間隔,避免 rate-limited
        await interruptible_sleep(state, 3)

    # ── 3. 寫入 DB ─────────────────────────────────────────────────
    if all_prices:
        n = await db.append_stock_prices(ts, all_prices)
        log.debug("stock: 寫入 %d 筆價格", n)
    else:
        log.warning("stock: 本次沒抓到任何價格 — 開 stock.log_raw_text 看原文")
        return

    # ── 4. 分析每支有歷史的股 ──────────────────────────────────────
    full_history = await db.load_stock_history(limit=10000)
    by_sym = group_by_symbol(full_history)

    signals: list[dict] = []
    for sym in all_prices:
        series = by_sym.get(sym, [])
        held_info = holdings.get(sym, {"shares": 0, "avg_cost": 0})
        result = analyze_symbol(
            sym, series,
            held_shares=held_info.get("shares", 0),
            avg_cost=held_info.get("avg_cost", 0),
            cfg=scfg,
        )
        signals.append(result)

        # 強訊號 → queue_log
        threshold = int(scfg.signal_score_threshold or 80)
        for eval_key in ("buy_eval", "sell_eval"):
            ev = result.get(eval_key)
            if ev is None:
                continue
            sig = ev.get("signal")
            sc = ev.get("score", 0)
            if sig in ("buy", "sell") and sc >= threshold:
                emoji = "🟢" if sig == "buy" else "🔴"
                msg = (f"{emoji} {sym} {sig.upper()} (score={sc}) "
                       f"@${ev.get('current', 0):.2f} — {ev.get('reason', '')[:80]}")
                state.queue_log(msg)
                log.info("stock signal: %s", msg)

    # ── 5. 把最新快照存到 state(讓 dashboard / UI 讀) ────────────
    async with state.lock:
        state.stock_last_snapshot = {
            "ts":       ts,
            "prices":   all_prices,
            "holdings": holdings,
            "signals":  signals,
        }
        state.stock_last_poll_ts = time.time()

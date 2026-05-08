"""股票監視 + 建議 loop。

每 N 分鐘:
1. 送 /stock 查全部股價 → parse → 寫 DB stock_prices
2. 送 /portfolio 查持股 → parse → 更新 stock_holdings
3. 對每支股(含未持有)用歷史價格做 buy/sell 分析
4. 強訊號(score ≥ threshold)寫進 state.queue_log + state.stock_signals
   (Phase 1-2:不會自動下單 — 純建議)
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
from bot.stock.parser import parse_holdings, parse_stock_prices

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
    # 啟動延遲(讓其他 loop 先穩定)
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

    # ── 1. 抓股價 ───────────────────────────────────────────────────
    log.info("stock: 查詢價格 (%s)", scfg.list_command)
    text = await query_stock_text(
        page, command=scfg.list_command, param=scfg.list_param,
    )
    if not text:
        log.warning("stock: %s 無回應", scfg.list_command)
        return

    if scfg.log_raw_text:
        log.info("stock raw text:\n%s", text[:2000])

    custom = [scfg.custom_price_pattern] if scfg.custom_price_pattern else None
    prices = parse_stock_prices(text, custom_patterns=custom)
    if not prices:
        log.warning("stock: 從回應抓不到任何 (symbol, price);"
                    "請開啟 stock.log_raw_text 看原文 + 調整 custom_price_pattern")
        return

    log.info("stock: 抓到 %d 支股票 — %s", len(prices),
             ", ".join(f"{s}=${p:.2f}" for s, p in list(prices.items())[:6]))
    n = await db.append_stock_prices(ts, prices)
    log.debug("stock: 寫入 %d 筆價格快照", n)

    # ── 2. 抓持股(獨立指令) ───────────────────────────────────────
    holdings = {}
    pf_text = await query_stock_text(
        page, command=scfg.portfolio_command, param=scfg.portfolio_param,
    )
    if pf_text:
        if scfg.log_raw_text:
            log.info("portfolio raw text:\n%s", pf_text[:2000])
        holdings = parse_holdings(pf_text)
        # 全部清掉重寫 — 保證已賣出的不會殘留
        await db.clear_stock_holdings()
        for sym, info in holdings.items():
            await db.upsert_stock_holding(
                sym, info["shares"], info["avg_cost"], ts,
            )
    log.info("stock: 持股 %d 支 — %s", len(holdings),
             ", ".join(f"{s}x{int(h['shares'])}" for s, h in list(holdings.items())[:6])
             or "(無)")

    # ── 3. 分析每支股 ───────────────────────────────────────────────
    # 拉最近 N=200 筆歷史(每 symbol)
    full_history = await db.load_stock_history(limit=5000)
    by_sym = group_by_symbol(full_history)

    signals: list[dict] = []
    for sym in prices:    # 只分析「目前還活著」的股票
        series = by_sym.get(sym, [])
        held = holdings.get(sym, {"shares": 0, "avg_cost": 0})
        result = analyze_symbol(
            sym, series,
            held_shares=held.get("shares", 0),
            avg_cost=held.get("avg_cost", 0),
            cfg=scfg,
        )
        signals.append(result)

        # 強訊號 → queue_log + ev counter
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

    # 把最新分析快照存到 state(讓 dashboard / UI 讀)
    async with state.lock:
        state.stock_last_snapshot = {
            "ts":       ts,
            "prices":   prices,
            "holdings": holdings,
            "signals":  signals,
        }
        state.stock_last_poll_ts = time.time()

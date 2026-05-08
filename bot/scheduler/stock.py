"""股票監視 + 建議 loop。

工作流程(每 poll_interval_min 分鐘):
1. **discover_all_stocks**:打 /stock 不送出,讀 autocomplete dropdown 一次拿
   所有 (symbol, price)。比挨支送 /stock symbol:X 快 N 倍。
2. **/portfolio**:抓持股(shares + avg_cost + 損益)。dropdown 已經有現價了。
3. 把所有價格寫進 stock_prices DB。
4. 對每支股做 buy/sell 建議。
5. 強訊號(score ≥ threshold)寫進 state.queue_log。

Phase 1-2:純建議,bot 不會自己下單。

Fallback:若 discovery 失敗(Discord DOM 變了等),回退用 /portfolio 抓持股價,
加上使用者自定的 tracked_symbols 各送一次 /stock symbol:X。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

from bot.core.state import BotState, interruptible_sleep
from bot.discord.client import (
    discover_all_stocks,
    query_stock_text,
)
from bot.stock.analysis import analyze_symbol, group_by_symbol
from bot.stock.parser import (
    parse_portfolio,
    parse_stock_detail,
    parse_stock_dropdown,
)

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
    # 啟動延遲
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

    # ── 1. 自動 discovery:讀 /stock symbol: 的 autocomplete dropdown ──
    log.info("stock: discover all stocks via autocomplete")
    dropdown_text = await discover_all_stocks(page, command=scfg.stock_command)
    if scfg.log_raw_text and dropdown_text:
        log.info("stock dropdown raw text:\n%s", dropdown_text[:2000])
    if dropdown_text:
        discovered = parse_stock_dropdown(dropdown_text)
        if discovered:
            log.info("stock: 自動抓到 %d 支 — %s", len(discovered),
                     ", ".join(f"{s}=${p:.2f}" for s, p in list(discovered.items())[:6]))
            all_prices.update(discovered)
        else:
            log.warning(
                "stock: discovery 抓到 dropdown 但 parser 無法解析(%d chars)— "
                "開 stock.log_raw_text 看 raw 文字並提供給開發者",
                len(dropdown_text),
            )
    else:
        log.warning("stock: discovery 失敗(autocomplete 沒回應或 DOM 變了),fallback")

    # ── 2. /portfolio:抓持股(shares + avg_cost) ─────────────────
    log.info("stock: 查 portfolio (%s)", scfg.portfolio_command)
    pf_text = await query_stock_text(page, command=scfg.portfolio_command)
    holdings: dict[str, dict] = {}
    if pf_text:
        if scfg.log_raw_text:
            log.info("portfolio raw text:\n%s", pf_text[:2000])
        holdings = parse_portfolio(pf_text)
        log.info("stock: portfolio 解析到 %d 支持股 — %s", len(holdings),
                 ", ".join(f"{s}×{int(h['shares'])}@{h['avg_cost']:.2f}"
                           for s, h in list(holdings.items())[:6]) or "(無)")
        # /portfolio 也帶現價 — 補進 all_prices(若 discovery 漏掉的)
        for sym, info in holdings.items():
            if info.get("current_price", 0) > 0:
                all_prices.setdefault(sym, info["current_price"])
        # 持股快照
        await db.clear_stock_holdings()
        for sym, info in holdings.items():
            await db.upsert_stock_holding(
                sym, info["shares"], info["avg_cost"], ts,
            )
    else:
        log.warning("stock: %s 無回應", scfg.portfolio_command)

    # ── 3. Fallback:tracked_symbols(若 discovery 失敗 + 使用者有設) ──
    if not dropdown_text and scfg.tracked_symbols:
        log.info("stock: discovery 失敗,fallback 對 %d 支 tracked symbols 各查",
                 len(scfg.tracked_symbols))
        for sym in scfg.tracked_symbols:
            sym = sym.upper().strip()
            if not sym or sym in all_prices:
                continue
            if state.quit:
                return
            try:
                stock_text = await query_stock_text(
                    page, command=scfg.stock_command, param=f"symbol: {sym}",
                )
                if stock_text:
                    detail = parse_stock_detail(stock_text, expected_symbol=sym)
                    if detail and detail.get("current_price", 0) > 0:
                        all_prices[detail["symbol"]] = detail["current_price"]
            except Exception:    # noqa: BLE001
                log.exception("stock: 查詢 %s 失敗", sym)
            await interruptible_sleep(state, 3)

    # ── 4. 寫入 DB ─────────────────────────────────────────────────
    if all_prices:
        n = await db.append_stock_prices(ts, all_prices)
        log.info("stock: 寫入 %d 支股票價格", n)
    else:
        log.warning("stock: 本次完全沒抓到價格 — discovery + portfolio 都失敗")
        return

    # ── 5. 分析每支股 ───────────────────────────────────────────────
    full_history = await db.load_stock_history(limit=20000)
    by_sym = group_by_symbol(full_history)

    signals: list[dict] = []
    for sym, cur_price in all_prices.items():
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
                       f"@{ev.get('current', 0):.2f} — {ev.get('reason', '')[:80]}")
                state.queue_log(msg)
                log.info("stock signal: %s", msg)

    # ── 6. 把最新快照存到 state ────────────────────────────────────
    async with state.lock:
        state.stock_last_snapshot = {
            "ts":         ts,
            "prices":     all_prices,
            "holdings":   holdings,
            "signals":    signals,
            "discovered": len(all_prices),
        }
        state.stock_last_poll_ts = time.time()

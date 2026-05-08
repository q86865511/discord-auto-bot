"""股票監視 + 建議 loop。

工作流程(每 poll_interval_min 分鐘):
1. **`/stock`(無 symbol)** → bot 回 embed 列出全部股票 → 一次拿到所有 (symbol, price)
2. **`/portfolio`** → 抓持股(shares + avg_cost + 損益)
3. 把所有價格寫進 stock_prices DB
4. 對每支股做 buy/sell 建議
5. 強訊號(score ≥ threshold)寫進 state.queue_log

Phase 1-2:純建議,bot 不會自己下單。
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
from bot.stock.parser import (
    parse_portfolio,
    parse_stock_detail,
    parse_stock_list,
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


def _trim_log(text: str | None, max_chars: int = 800) -> str:
    """為了 log_raw_text 把超長文字截掉中間,只留尾巴(回應通常在最後)。"""
    if not text:
        return "(空)"
    if len(text) <= max_chars:
        return text
    # 留尾巴,前面用 [...] 表示截掉了
    return f"[...前 {len(text) - max_chars} 字省略]\n{text[-max_chars:]}"


async def _poll_once(page, state: BotState, scfg, db) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_prices: dict[str, float] = {}

    # ── 1. /stock(無 symbol) → bot 回 embed 列出全部股票 ───────────
    log.info("stock: 查 stock list (%s)", scfg.stock_command)
    list_text = await query_stock_text(page, command=scfg.stock_command)
    if scfg.log_raw_text:
        log.info("stock list raw text (last 800 chars):\n%s",
                 _trim_log(list_text, 800))
    if list_text:
        discovered = parse_stock_list(list_text)
        if discovered:
            log.info("stock: 抓到 %d 支 — %s", len(discovered),
                     ", ".join(f"{s}=${p:.2f}"
                               for s, p in list(discovered.items())[:6]))
            all_prices.update(discovered)
        else:
            log.warning(
                "stock: 從 /stock 回應抓不到任何 symbol;"
                "開 stock.log_raw_text 看 raw 文字校準 parser",
            )
    else:
        log.warning("stock: %s 無回應", scfg.stock_command)

    await interruptible_sleep(state, 2)

    # ── 2. /portfolio:抓持股(shares + avg_cost + 現價) ──────────
    log.info("stock: 查 portfolio (%s)", scfg.portfolio_command)
    pf_text = await query_stock_text(page, command=scfg.portfolio_command)
    holdings: dict[str, dict] = {}
    if pf_text:
        if scfg.log_raw_text:
            log.info("portfolio raw text (last 800 chars):\n%s",
                     _trim_log(pf_text, 800))
        holdings = parse_portfolio(pf_text)
        log.info("stock: portfolio 解析到 %d 支持股 — %s", len(holdings),
                 ", ".join(f"{s}×{int(h['shares'])}@{h['avg_cost']:.2f}"
                           for s, h in list(holdings.items())[:6]) or "(無)")
        # /portfolio 的現價優先(更新),沒抓到的就用 /stock list 那邊的
        for sym, info in holdings.items():
            if info.get("current_price", 0) > 0:
                all_prices[sym] = info["current_price"]
        # 持股快照
        await db.clear_stock_holdings()
        for sym, info in holdings.items():
            await db.upsert_stock_holding(
                sym, info["shares"], info["avg_cost"], ts,
            )
    else:
        log.warning("stock: %s 無回應", scfg.portfolio_command)

    # ── 3. 備援:對 tracked_symbols 各送一次 /stock symbol:X ──────
    # 通常用不到,只在 /stock 主清單抓不到時補
    if scfg.tracked_symbols and len(all_prices) < 2:
        log.info("stock: 主清單只抓到 %d 支,fallback tracked_symbols (%d 支)",
                 len(all_prices), len(scfg.tracked_symbols))
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

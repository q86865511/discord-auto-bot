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

from bot.core.state import (
    BotState,
    interruptible_sleep,
    is_loop_auto_paused,
    mark_loop_failed,
    mark_loop_ok,
    mark_loop_running,
)
from bot.discord.client import (
    query_portfolio_full,
    query_stock_text,
)
from bot.notifications.digest import notify_stock_signal, notify_stock_volatility
from bot.stock.analysis import analyze_symbol, detect_volatility, group_by_symbol
from bot.stock.parser import (
    parse_portfolio,
    parse_portfolio_shorts,
    parse_portfolio_summary,
    parse_stock_detail,
    parse_stock_list_with_trend,
)

# Loop name(state.loop_health 的 key)
_LOOP_NAME = "stock"
# auto_paused 後等多久再試
_PAUSE_RECOVERY_SEC = 30 * 60

if TYPE_CHECKING:
    from playwright.async_api import Page

    from bot.core.config import BotConfig
    from bot.core.db import Database

log = logging.getLogger(__name__)


def _dump_parse_debug(label: str, text: str) -> None:
    """parser 抓不到時,把原文寫到獨立 debug 檔(不污染 bot.log)。

    每次蓋寫(不累積),只保留最後一次失敗的 raw text — 方便對照原始
    embed 校準 parser regex,但又不會把 logs/bot.log 撐爆。
    """
    import os
    try:
        os.makedirs("logs", exist_ok=True)
        with open("logs/stock_debug.log", "w", encoding="utf-8") as f:
            f.write(f"=== {label} parse failed @ {datetime.now()} ===\n")
            f.write(f"length: {len(text)} chars\n")
            f.write("--- last 2000 chars ---\n")
            f.write(text[-2000:])
            f.write("\n=== end ===\n")
    except OSError as e:
        log.debug("無法寫 stock_debug.log: %s", e)


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
        cfg = config_provider()
        scfg = cfg.stock
        if not scfg.enabled:
            await interruptible_sleep(state, 60)
            continue

        # 一進迴圈就消費 force_poll 旗標(若 sleep 期間被設,這裡 reset)
        forced = state.stock_force_poll
        if forced:
            state.stock_force_poll = False

        # 連續失敗達閾值 → 進入 auto_paused 冷卻;sleep 30 分鐘再試一次
        # (force_poll 可以 override 冷卻,讓 user 主動測試恢復)
        if is_loop_auto_paused(state, _LOOP_NAME) and not forced:
            log.warning("stock: 連續失敗已達閾值,冷卻 %d 秒後重試",
                        _PAUSE_RECOVERY_SEC)
            state.queue_log("⛔ 股票 loop 連續失敗,已自動暫停 30 分鐘")
            # 切成 chunks 讓 force_poll 能中斷
            slept = 0
            while slept < _PAUSE_RECOVERY_SEC and not state.quit:
                chunk = min(30, _PAUSE_RECOVERY_SEC - slept)
                await interruptible_sleep(state, chunk)
                slept += chunk
                if state.stock_force_poll:
                    state.stock_force_poll = False
                    log.info("stock: force_poll 中斷 auto_paused 冷卻")
                    break
            if state.quit:
                break

        mark_loop_running(state, _LOOP_NAME)
        ok = False
        try:
            ok = await _poll_once(page, state, cfg, db)
        except Exception as e:    # noqa: BLE001
            log.exception("stock loop 例外")
            mark_loop_failed(state, _LOOP_NAME, str(e))
        else:
            if ok:
                mark_loop_ok(state, _LOOP_NAME)
            else:
                mark_loop_failed(state, _LOOP_NAME,
                                 "discovery + portfolio 都沒抓到資料")
                state.queue_log("⚠ 股票 poll 完全沒抓到資料")

        # Sleep 切成 30 秒 chunks — 每 chunk 結束 check force_poll 旗標,
        # 讓 UI / Dashboard 觸發的「立即重 poll」最多 30 秒內生效。
        sleep_sec = max(60, int(float(scfg.poll_interval_min) * 60))
        slept = 0
        while slept < sleep_sec and not state.quit:
            chunk = min(30, sleep_sec - slept)
            await interruptible_sleep(state, chunk)
            slept += chunk
            if state.stock_force_poll:
                log.info("stock: 收到 force_poll,跳出 sleep 立即重 poll")
                break


async def _poll_once(page, state: BotState, cfg, db) -> bool:
    """單次股票 poll。回傳 True = 成功(至少抓到 prices 或 holdings),
    False = 兩邊都沒抓到(視為失敗,呼叫端會 mark_loop_failed)。

    重要:用 try/finally 確保即使中段拋例外,snapshot 也會用「目前累積到的
    最新資料」更新。修先前的 bug:賣股後若 analyze 階段失敗,snapshot 仍是
    舊的,UI 顯示已賣股票仍在「持股區」。
    """
    scfg = cfg.stock
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    all_prices: dict[str, float] = {}
    all_trends: dict[str, float] = {}     # symbol → 趨勢 %(從 /stock 抓)
    holdings: dict[str, dict] = {}
    shorts: dict[str, dict] = {}          # 做空倉位
    portfolio_summary: dict = {}          # 組合盈虧 / 總未實現盈虧
    signals: list[dict] = []
    portfolio_parsed = False    # 是否成功 parse 過 portfolio(就算空)

    try:
        # ── 1. /stock(無 symbol) → bot 回 embed 列出全部股票 + 趨勢 ──
        log.info("stock: 查 stock list (%s)", scfg.stock_command)
        list_text = await query_stock_text(page, command=scfg.stock_command)
        if list_text:
            discovered = parse_stock_list_with_trend(list_text)
            if discovered:
                log.info("stock: 抓到 %d 支 — %s", len(discovered),
                         ", ".join(f"{s}=${info['price']:.2f}"
                                   for s, info in list(discovered.items())[:6]))
                for sym, info in discovered.items():
                    all_prices[sym] = info["price"]
                    if info.get("trend_pct") is not None:
                        all_trends[sym] = info["trend_pct"]
            else:
                _dump_parse_debug("stock_list", list_text)
                log.warning(
                    "stock: 從 /stock 回應抓不到 symbol — "
                    "原文已寫到 logs/stock_debug.log",
                )
        else:
            log.warning("stock: %s 無回應", scfg.stock_command)

        await interruptible_sleep(state, 2)

        # ── 2. /portfolio:抓持股 + 做空倉位 + 摘要 ───────────────────
        log.info("stock: 查 portfolio + 做空 (%s)", scfg.portfolio_command)
        pf_text, shorts_text = await query_portfolio_full(
            page, portfolio_command=scfg.portfolio_command,
        )
        if pf_text:
            holdings = parse_portfolio(pf_text)
            portfolio_summary = parse_portfolio_summary(pf_text)
            portfolio_parsed = True    # 即使 holdings={} 也算成功(全部賣完)
            # 對比上次 snapshot,偵測買賣 — 讓 user 立即看到 bot 有抓到變動,
            # 不用對著「持股區還有那支」來懷疑 bot 沒更新
            prev_snap = state.stock_last_snapshot or {}
            prev_keys = set(prev_snap.get("holdings", {}).keys())
            cur_keys = set(holdings.keys())
            sold = prev_keys - cur_keys
            bought = cur_keys - prev_keys
            if sold:
                state.queue_log(f"💰 偵測賣出: {', '.join(sorted(sold))}")
                log.info("stock: 偵測賣出 %s", sorted(sold))
            if bought:
                state.queue_log(f"🛒 偵測買進: {', '.join(sorted(bought))}")
                log.info("stock: 偵測買進 %s", sorted(bought))
            if not holdings:
                _dump_parse_debug("portfolio", pf_text)
                log.info("stock: portfolio 解析到 0 支持股(已全部賣完?)")
            else:
                log.info("stock: portfolio 解析到 %d 支持股 — %s",
                         len(holdings),
                         ", ".join(f"{s}×{int(h['shares'])}@{h['avg_cost']:.2f}"
                                   for s, h in list(holdings.items())[:6]))
            # /portfolio 的現價優先(更新),沒抓到的就用 /stock list 那邊的
            for sym, info in holdings.items():
                if info.get("current_price", 0) > 0:
                    all_prices[sym] = info["current_price"]
            # 持股快照(DB 層)— 即使 holdings 空也要 clear,確保賣完那支從 DB 移除
            await db.clear_stock_holdings()
            for sym, info in holdings.items():
                await db.upsert_stock_holding(
                    sym, info["shares"], info["avg_cost"], ts,
                )
        else:
            log.warning("stock: %s 無回應", scfg.portfolio_command)

        # ── 2b. 做空倉位(從 /portfolio 點「做空倉位」button 之後) ───
        if shorts_text:
            shorts = parse_portfolio_shorts(shorts_text)
            if shorts:
                log.info("stock: 做空 %d 支 — %s", len(shorts),
                         ", ".join(f"{s}×{int(d['shares'])}@{d['avg_short_price']:.2f}"
                                   for s, d in list(shorts.items())[:6]))
                # 做空也有現價可用
                for sym, d in shorts.items():
                    if d.get("current_price", 0) > 0:
                        all_prices.setdefault(sym, d["current_price"])
            else:
                log.info("stock: 做空畫面解析到 0 支(可能無做空倉位)")
        elif pf_text:
            # 主畫面有,做空 button 沒點到 → 多半是沒做空,不算錯誤
            log.debug("stock: 沒點到做空 button,跳過做空 parse")

        # ── 3. 備援:對 tracked_symbols 各送一次 /stock symbol:X ──
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
                    # Tab 後 cursor 已在 symbol 參數欄,直接打 value(打
                    # "symbol:..." 會變成 /stock symbol: symbol:X — bot 看不懂)
                    stock_text = await query_stock_text(
                        page, command=scfg.stock_command, param=sym,
                    )
                    if stock_text:
                        detail = parse_stock_detail(
                            stock_text, expected_symbol=sym,
                        )
                        if detail and detail.get("current_price", 0) > 0:
                            all_prices[detail["symbol"]] = detail["current_price"]
                except Exception:    # noqa: BLE001
                    log.exception("stock: 查詢 %s 失敗", sym)
                await interruptible_sleep(state, 3)

        # ── 4. Sanity check + 寫入 DB ──────────────────────────────
        # 防 parser 異常把 A 股票的價格寫到 B 股票(例如 MAID 27000 跑到
        # WAVE 上,觸發「暴漲 43000%」的鬧劇)。比較上次 snapshot 的價格,
        # 同 sym 變動超過 10x 就棄用新值,保留舊值(寬鬆 ratio 允許正常
        # 漲跌停的 ±10~30% 波動,但擋掉 100x 以上的離譜跳動)。
        prev_prices = (state.stock_last_snapshot or {}).get("prices", {}) or {}
        cleaned: dict[str, float] = {}
        for sym, p in all_prices.items():
            prev = prev_prices.get(sym)
            if prev is not None and prev > 0 and p > 0:
                ratio = max(p, prev) / min(p, prev)
                if ratio > 10.0:
                    log.warning(
                        "stock: %s 價格從 %.4f 跳到 %.4f (%.1fx) — "
                        "疑似 parser 異常,棄用此次值",
                        sym, prev, p, ratio,
                    )
                    state.queue_log(
                        f"⚠ {sym} 價格異常跳動 ({prev:.2f}→{p:.2f}),棄用"
                    )
                    cleaned[sym] = prev
                    continue
            cleaned[sym] = p
        all_prices = cleaned

        if all_prices:
            n = await db.append_stock_prices(ts, all_prices)
            log.info("stock: 寫入 %d 支股票價格", n)
        else:
            log.warning("stock: 本次完全沒抓到價格 — discovery + portfolio 都失敗")
            # 沒價格也沒 portfolio = 完全失敗 → 呼叫端 mark_failed
            # finally 仍會更新 holdings(若 portfolio 有 parse 過,return True)
            return portfolio_parsed

        # ── 5. 分析每支股 ───────────────────────────────────────────
        full_history = await db.load_stock_history(limit=20000)
        by_sym = group_by_symbol(full_history)

        threshold = int(scfg.signal_score_threshold or 80)
        # 本輪偵測到的強訊號 → 跟 state.stock_notified_signals 比對 → 寄 email
        current_strong: dict[tuple[str, str], int] = {}

        for sym, _cur_price in all_prices.items():
            series = by_sym.get(sym, [])
            held_info = holdings.get(sym, {"shares": 0, "avg_cost": 0})
            result = analyze_symbol(
                sym, series,
                held_shares=held_info.get("shares", 0),
                avg_cost=held_info.get("avg_cost", 0),
                cfg=scfg,
            )
            signals.append(result)

            # 強訊號 → queue_log + (anti-spam) email
            for eval_key in ("buy_eval", "sell_eval"):
                ev = result.get(eval_key)
                if ev is None:
                    continue
                sig = ev.get("signal")
                sc = ev.get("score", 0)
                if sig in ("buy", "sell") and sc >= threshold:
                    emoji = "🟢" if sig == "buy" else "🔴"
                    msg = (f"{emoji} {sym} {sig.upper()} (score={sc}) "
                           f"@{ev.get('current', 0):.2f} — "
                           f"{ev.get('reason', '')[:80]}")
                    state.queue_log(msg)
                    log.info("stock signal: %s", msg)
                    key = (sym, sig)
                    current_strong[key] = sc
                    # Email anti-spam:只在「之前沒通知過 / 之前分數較低」時寄
                    already = state.stock_notified_signals.get(key)
                    if already is None or sc > already + 5:
                        try:
                            await notify_stock_signal(state, cfg, sym, sig, ev)
                        except Exception:    # noqa: BLE001
                            log.exception("notify_stock_signal 失敗")
                        async with state.lock:
                            state.stock_notified_signals[key] = sc

        # 移除已不再強的訊號(訊號消失 → 下次再出現要重新通知)
        async with state.lock:
            for key in list(state.stock_notified_signals.keys()):
                if key not in current_strong:
                    del state.stock_notified_signals[key]

        # ── 5b. 短期波動警示(獨立於 buy/sell 訊號) ──────────────
        if scfg.volatility_alert_enabled:
            await _check_volatility(state, cfg, by_sym)

        # 注:新聞抓取已搬到獨立 news_loop(bot/scheduler/news.py),
        # cadence 由 stock.news_poll_interval_min 控制(預設 60 分鐘)。

    finally:
        # ── 6. 寫 snapshot — 不論 try 區塊是否拋例外,都用「目前累積到的
        #      最新資料」更新。修先前 bug:賣股後若 analyze 中途失敗,
        #      snapshot 仍是舊的,UI 顯示已賣股票。現在只要 portfolio 成功
        #      parse 過(即使 holdings={} 表示已全部賣完),持股區就會更新。
        # 完全沒抓到任何東西就保留舊 snapshot(避免 query 暫時失敗時 UI
        # 整個變空)。
        if portfolio_parsed or all_prices:
            async with state.lock:
                state.stock_last_snapshot = {
                    "ts":         ts,
                    "prices":     all_prices,
                    "trends":     all_trends,           # 新:股票趨勢 %
                    "holdings":   holdings,
                    "shorts":     shorts,               # 新:做空倉位
                    "summary":    portfolio_summary,    # 新:組合盈虧摘要
                    "signals":    signals,
                    "discovered": len(all_prices),
                }
                state.stock_last_poll_ts = time.time()
    return True


async def _check_volatility(state: BotState, cfg, by_sym: dict[str, list]) -> None:
    """對每支抓到的股檢查短期波動。

    在 stock_loop 5b 階段呼叫。不是 analysis 部分(那是 MA / 獲利率啟發),
    這純粹比較最近 N 分鐘內的價格百分比變動。
    """
    scfg = cfg.stock
    win = float(scfg.volatility_window_min or 30)
    thr = float(scfg.volatility_threshold_pct or 5.0)
    cooldown_sec = float(scfg.volatility_cooldown_min or 60) * 60.0
    now_ts = time.time()

    for sym, series in by_sym.items():
        info = detect_volatility(series, win, thr)
        if info is None:
            continue
        direction = info["direction"]    # "rise" / "fall"
        change = info["change_pct"]
        key = (sym, direction)
        # 同 sym 同方向 cooldown 內只通知一次
        last_ts = state.stock_volatility_notified.get(key, 0)
        if now_ts - last_ts < cooldown_sec:
            continue

        emoji = "📈" if direction == "rise" else "📉"
        label = "暴漲" if direction == "rise" else "暴跌"
        msg = (f"{emoji} {sym} {label} {change:+.2f}% / 過去 "
               f"{win:g} min @ {info['current']:.2f}")
        state.queue_log(msg)
        log.info("stock volatility: %s", msg)
        try:
            await notify_stock_volatility(state, cfg, sym, info)
        except Exception:    # noqa: BLE001
            log.exception("notify_stock_volatility 失敗")
        async with state.lock:
            state.stock_volatility_notified[key] = now_ts

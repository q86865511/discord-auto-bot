"""股票新聞抓取 loop — 獨立於 stock_loop 的 cadence。

對「全部抓到的股票」(state.stock_last_snapshot.prices.keys())序列送
`/stock symbol:X` → 點「近期新聞」按鈕 → parse 新聞 → 寫 DB 去重 →
新項 queue_log + email。

跟 stock_loop 完全分開:
- stock_loop 跑 poll_interval_min(預設 15 分鐘)抓價格 + 分析
- news_loop 跑 news_poll_interval_min(預設 60 分鐘)抓新聞

新聞變動不那麼頻繁,獨立 loop 避免 stock cadence 被新聞抓取(序列 + 每
支 3 秒間隔)拖累。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from bot.core.state import (
    BotState,
    interruptible_sleep,
    mark_loop_failed,
    mark_loop_ok,
    mark_loop_running,
    wait_while_paused,
)
from bot.discord.client import query_stock_news
from bot.notifications.digest import notify_stock_news
from bot.stock.parser import parse_stock_news

if TYPE_CHECKING:
    from playwright.async_api import Page

    from bot.core.config import BotConfig
    from bot.core.db import Database

log = logging.getLogger(__name__)

_LOOP_NAME = "news"


async def news_loop(
    page: "Page", state: BotState,
    config_provider: Callable[[], "BotConfig"],
    on_config_save: Callable[["BotConfig"], Awaitable[None]],   # noqa: ARG001
    db: "Database",
) -> None:
    """股票新聞抓取主 loop。"""
    # 啟動延遲:等 stock_loop 至少跑一次 poll(60s + 第一次 poll 抓完)
    # 讓 snapshot.prices 有資料,我們才知道要抓哪些 sym
    async with state.lock:
        state.news_next_poll_ts = time.time() + 90
    await interruptible_sleep(state, 90)

    # 啟動先 load DB 既有新聞到 state(UI 立刻有資料)
    try:
        recent = await db.load_recent_news(limit=5)
        async with state.lock:
            state.stock_recent_news = recent
        log.info("news loop: 從 DB 載入 %d 則既有新聞", len(recent))
    except Exception:    # noqa: BLE001
        log.exception("初次 load 新聞失敗(可忽略)")

    while not state.quit:
        await wait_while_paused(state)
        if state.quit:
            break

        cfg = config_provider()
        scfg = cfg.stock
        if not scfg.enabled:
            await interruptible_sleep(state, 60)
            continue

        # 如果 stock snapshot 還沒 ready(剛啟動 stock_loop 還在跑第一次 poll),
        # 短 sleep 30 秒後 retry — 而非整個 news_poll_interval(預設 60 分鐘)
        # 才再試。避免 bot 啟動後 1 小時都沒新聞。
        snap = state.stock_last_snapshot or {}
        if not (snap.get("prices") or {}):
            log.info("news loop: stock snapshot 還沒 ready,30 秒後 retry")
            async with state.lock:
                state.news_next_poll_ts = time.time() + 30
            await interruptible_sleep(state, 30)
            continue

        mark_loop_running(state, _LOOP_NAME)
        try:
            await _check_all_news(page, state, cfg, db)
            mark_loop_ok(state, _LOOP_NAME)
        except Exception as e:    # noqa: BLE001
            log.exception("news loop 例外")
            mark_loop_failed(state, _LOOP_NAME, str(e))

        # Sleep until next news poll;最少 5 分鐘。設 news_next_poll_ts 讓
        # UI 能顯示倒數
        sleep_sec = max(5 * 60, int(float(scfg.news_poll_interval_min) * 60))
        async with state.lock:
            state.news_next_poll_ts = time.time() + sleep_sec
        await interruptible_sleep(state, sleep_sec)


async def _check_all_news(
    page: "Page", state: BotState, cfg: "BotConfig", db: "Database",
) -> None:
    """對 snapshot.prices 中所有 sym 抓新聞,新項 queue_log + email。

    序列抓取(每支間隔 3 秒,避免 spam Discord)。新聞 DB 用 UNIQUE
    (symbol, date, title) 去重 — 只有真的「新加入」才觸發通知。

    caller(news_loop)已保證 snapshot.prices 非空才會呼叫進來。
    """
    snap = state.stock_last_snapshot or {}
    all_syms = sorted((snap.get("prices") or {}).keys())

    log.info("news loop: 開始抓 %d 支(%s)", len(all_syms),
             ", ".join(all_syms[:10]))
    all_new_items: list[dict] = []
    for sym in all_syms:
        if state.quit:
            return
        try:
            news_text = await query_stock_news(
                page, sym, stock_command=cfg.stock.stock_command,
            )
            if not news_text:
                continue
            items = parse_stock_news(news_text, expected_symbol=sym)
            if not items:
                continue
            new_items = await db.upsert_news_items(items)
            if new_items:
                all_new_items.extend(new_items)
                log.info("news loop: %s 新增 %d 則(總抓 %d)",
                         sym, len(new_items), len(items))
        except Exception:    # noqa: BLE001
            log.exception("news loop: 抓 %s 失敗", sym)
        await interruptible_sleep(state, 3)

    # 載最近 5 筆 cross-sym(按 news_date DESC + id DESC)給 UI
    try:
        recent = await db.load_recent_news(limit=5)
        async with state.lock:
            state.stock_recent_news = recent
    except Exception:    # noqa: BLE001
        log.exception("載入 recent news 失敗")

    # 新項 → queue_log + email
    if all_new_items:
        for it in all_new_items[:10]:    # 限制 queue_log 數量
            title = it["title"][:60]
            state.queue_log(
                f"📰 {it['symbol']} ({it['date']}) {title}"
            )
        try:
            await notify_stock_news(state, cfg, all_new_items)
        except Exception:    # noqa: BLE001
            log.exception("notify_stock_news 失敗")

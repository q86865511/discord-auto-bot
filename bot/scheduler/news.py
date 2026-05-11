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
from bot.discord.client import (
    _query_stock_news_no_lock,
    channel_context,
)
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
        # UI 能顯示倒數。分塊 sleep 讓 force_poll 30 秒內生效(user 在設定
        # [D] 清空 DB 後想立刻看新聞,不用等 60 分鐘)
        sleep_sec = max(5 * 60, int(float(scfg.news_poll_interval_min) * 60))
        async with state.lock:
            state.news_next_poll_ts = time.time() + sleep_sec
        slept = 0
        while slept < sleep_sec and not state.quit:
            chunk = min(30, sleep_sec - slept)
            await interruptible_sleep(state, chunk)
            slept += chunk
            if state.news_force_poll:
                async with state.lock:
                    state.news_force_poll = False
                log.info("news loop: 收到 force_poll,跳出 sleep 立即重跑")
                break


async def _check_all_news(
    page: "Page", state: BotState, cfg: "BotConfig", db: "Database",
) -> None:
    """對 snapshot.prices 中所有 sym 抓新聞,新項 queue_log + email。

    Strategy:全程持 command_lock(獨佔 page),整段 navigate 到 news channel
    →序列抓全 sym(每支 3 秒間隔)→ navigate 回主 channel。這樣其他 loop
    不會在中段送指令到 news channel,也不會干擾 page.textContent。

    caller(news_loop)已保證 snapshot.prices 非空才會呼叫進來。
    """
    snap = state.stock_last_snapshot or {}
    all_syms = sorted((snap.get("prices") or {}).keys())

    news_ch_id = (cfg.stock.news_channel_id or "").strip()

    log.info("news loop: 開始抓 %d 支(%s)%s", len(all_syms),
             ", ".join(all_syms[:10]),
             f" — 切到新聞頻道 {news_ch_id}" if news_ch_id else "")
    # 主面板 queue_log 一條,user 不在 T 鍵頁也能立刻看到 cycle 啟動
    state.queue_stock_log(
        f"🌐 news cycle 開始 — 抓 {len(all_syms)} 支 "
        f"({', '.join(all_syms[:6])}{'...' if len(all_syms) > 6 else ''})"
    )
    all_new_items: list[dict] = []

    # 用 channel_context 統一管理:持 command_lock + navigate news_channel +
    # 完成後切回主頻道(channel_context 自動處理 navigate-back)
    async with channel_context(page, state, news_ch_id):
        stats = {"ok": 0, "no_text": 0, "no_items": 0, "no_new": 0, "exc": 0}
        for sym in all_syms:
            if state.quit:
                break
            try:
                news_text, ephemeral_iso_ts = await _query_stock_news_no_lock(
                    page, sym, stock_command=cfg.stock.stock_command,
                )
                if not news_text:
                    log.warning(
                        "news loop: %s 沒抓到 news_text "
                        "(detail/news marker timeout 或 button 點不到)",
                        sym,
                    )
                    stats["no_text"] += 1
                    continue
                items = parse_stock_news(news_text, expected_symbol=sym)
                if not items:
                    log.warning(
                        "news loop: %s parse 後 0 items "
                        "(sanity 棄用,可能 ephemeral 累積 + rfind 失敗)",
                        sym,
                    )
                    stats["no_items"] += 1
                    continue
                # 把 ephemeral 訊息的 ISO ts 寫進 fetched_ts
                if ephemeral_iso_ts:
                    try:
                        from datetime import datetime as _dt
                        t = _dt.fromisoformat(
                            ephemeral_iso_ts.replace("Z", "+00:00")
                        ).astimezone()
                        iso_clean = t.strftime("%Y-%m-%d %H:%M:%S")
                        for it in items:
                            it["fetched_ts"] = iso_clean
                    except (ValueError, TypeError):
                        pass
                new_items = await db.upsert_news_items(items)
                if new_items:
                    all_new_items.extend(new_items)
                    log.info("news loop: %s 新增 %d 則(總抓 %d)",
                             sym, len(new_items), len(items))
                    stats["ok"] += 1
                else:
                    log.info("news loop: %s 抓到 %d 則但全部已存在",
                             sym, len(items))
                    stats["no_new"] += 1
            except Exception:    # noqa: BLE001
                log.exception("news loop: 抓 %s 失敗", sym)
                stats["exc"] += 1
            from bot.core.state import interruptible_sleep as _is
            await _is(state, 3)

        # cycle 末總結 log
        log.info(
            "news loop: cycle 完成 — ok=%d no_text=%d no_items=%d "
            "no_new=%d exc=%d(共 %d 支)",
            stats["ok"], stats["no_text"], stats["no_items"],
            stats["no_new"], stats["exc"], len(all_syms),
        )
        if stats["no_text"] + stats["no_items"] + stats["exc"] > 0:
            state.queue_stock_log(
                f"⚠ news cycle:{stats['ok']}/{len(all_syms)} 成功,"
                f"{stats['no_text']} 沒文字 / {stats['no_items']} 棄用 / "
                f"{stats['exc']} 例外 — X 鍵看細節"
            )

    # 載最近 5 筆 cross-sym(按 news_date DESC + id DESC)給 UI
    try:
        recent = await db.load_recent_news(limit=5)
        async with state.lock:
            state.stock_recent_news = recent
    except Exception:    # noqa: BLE001
        log.exception("載入 recent news 失敗")

    # 新項 → queue_log + email
    if all_new_items:
        for it in all_new_items[:10]:
            title = it["title"][:60]
            state.queue_stock_log(
                f"📰 {it['symbol']} ({it['date']}) {title}"
            )
        try:
            await notify_stock_news(state, cfg, all_new_items)
        except Exception:    # noqa: BLE001
            log.exception("notify_stock_news 失敗")

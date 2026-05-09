"""/daily 領取 loop — 錨定到每天 00:00 觸發。"""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from bot.core.state import (
    BotState,
    interruptible_sleep,
    mark_loop_failed,
    mark_loop_ok,
    mark_loop_running,
    wait_while_paused,
)
from bot.discord.client import send_and_capture_balance
from bot.notifications.digest import maybe_notify_goal

if TYPE_CHECKING:
    from playwright.async_api import Page

    from bot.core.config import BotConfig
    from bot.core.db import Database

log = logging.getLogger(__name__)

# DB meta key — 記錄上次 /daily 觸發的 epoch 秒,用來判斷今天是否已跑過
_LAST_DAILY_META_KEY = "last_daily_fired_ts"


def _seconds_until_next_midnight(now: datetime | None = None) -> float:
    """下一個 00:00 的秒數(隔日的午夜)。"""
    now = now or datetime.now()
    next_midnight = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                     + timedelta(days=1))
    return max(0.0, (next_midnight - now).total_seconds())


def _today_midnight_ts(now: datetime | None = None) -> float:
    """今天 00:00:00 的 epoch 秒。"""
    now = now or datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


async def daily_loop(
    page: "Page", state: BotState,
    config_provider: Callable[[], "BotConfig"],
    on_config_save: Callable[["BotConfig"], Awaitable[None]],
    db: "Database",
) -> None:
    """/daily 領取 — 每天 00:00 觸發 + 30~120 秒 jitter(避開 server reset 抖動)。

    跨重啟防重複:用 DB meta key `last_daily_fired_ts` 記錄上次觸發時間。
    啟動時若「今日尚未跑過」(last_fired < 今天 00:00) → 補跑;否則 sleep
    到明天 00:00。

    為什麼是 server reset 後 30~120 秒?太準會撞「reset 中」的回應(收不到
    新餘額),稍延一點較穩,jitter 也讓 bot 看起來不那麼機械。
    """
    while not state.quit:
        await wait_while_paused(state)
        if state.quit:
            break

        # 判斷今天是否已跑過:讀 DB meta 比較今天 00:00
        last_fired_str = await db.get_meta(_LAST_DAILY_META_KEY)
        try:
            last_fired = float(last_fired_str) if last_fired_str else 0.0
        except (TypeError, ValueError):
            last_fired = 0.0
        today_mid = _today_midnight_ts()

        if last_fired < today_mid:
            # 今天還沒跑(或第一次啟動)— 立刻補跑(留 5~15 秒讓其他 init 完成)
            jitter = random.uniform(5, 15)
            async with state.lock:
                state.daily_next = time.time() + jitter
            log.info("/daily 今天尚未執行,將在 %.0f 秒後補跑", jitter)
            await interruptible_sleep(state, jitter)
        else:
            # 今天已跑過 — sleep 到下個 00:00 + 30~120 秒 jitter
            wait_to_midnight = _seconds_until_next_midnight()
            jitter = random.uniform(30.0, 120.0)
            delay = wait_to_midnight + jitter
            async with state.lock:
                state.daily_next = time.time() + delay
            log.info("下次 /daily 在 %.0f 秒後(下個 00:00 +%.0fs jitter)",
                     delay, jitter)
            await interruptible_sleep(state, delay)

        if state.quit:
            break
        await wait_while_paused(state)
        if state.quit:
            break

        mark_loop_running(state, "daily")
        try:
            new_bal = await send_and_capture_balance(
                page, "/daily", timeout=20.0, stability_sec=2.0,
            )
        except Exception as e:    # noqa: BLE001
            log.exception("/daily 送出時例外")
            mark_loop_failed(state, "daily", str(e))
            new_bal = None
        else:
            mark_loop_ok(state, "daily")
        # 不論成功失敗都記錄 — 避免時間沒到回 None 時 loop 又重試把整天嘗試光
        await db.set_meta(_LAST_DAILY_META_KEY, str(time.time()))
        if new_bal is not None:
            async with state.lock:
                state.balance = new_bal
                state.events.daily_claims += 1
            log.info("/daily 完成,餘額更新為 %d", new_bal)
            await maybe_notify_goal(page, state, config_provider(), on_config_save)
        else:
            log.info("/daily 已送出(未取得新餘額,可能伺服器 reset 慢一點)")

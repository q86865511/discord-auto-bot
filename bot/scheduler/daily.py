"""/daily 領取 loop — 24h ± jitter。"""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from bot.core.constants import (
    DAILY_BASE_SEC,
    DAILY_JITTER_SEC,
    DAILY_STARTUP_DELAY_SEC,
)
from bot.core.state import BotState, interruptible_sleep, wait_while_paused
from bot.discord.client import send_and_capture_balance
from bot.notifications.digest import maybe_notify_goal

if TYPE_CHECKING:
    from playwright.async_api import Page

    from bot.core.config import BotConfig

log = logging.getLogger(__name__)


async def daily_loop(
    page: "Page", state: BotState,
    config_provider: Callable[[], "BotConfig"],
    on_config_save: Callable[["BotConfig"], Awaitable[None]],
) -> None:
    """/daily 領取。啟動隨機延遲 0~5 分鐘,後續 24h ± 45min jitter。"""
    startup = random.uniform(0, DAILY_STARTUP_DELAY_SEC)
    async with state.lock:
        state.daily_next = time.time() + startup
    log.info("/daily 將在 %.0f 秒後送出第一次", startup)
    await interruptible_sleep(state, startup)

    while not state.quit:
        await wait_while_paused(state)
        if state.quit:
            break
        new_bal = await send_and_capture_balance(
            page, "/daily", timeout=20.0, stability_sec=2.0,
        )
        if new_bal is not None:
            async with state.lock:
                state.balance = new_bal
                state.events.daily_claims += 1
            log.info("/daily 完成,餘額更新為 %d", new_bal)
            await maybe_notify_goal(page, state, config_provider(), on_config_save)
        else:
            log.info("/daily 已送出(未取得新餘額,可能尚未到時間)")
        delay = DAILY_BASE_SEC + random.uniform(-DAILY_JITTER_SEC, DAILY_JITTER_SEC)
        async with state.lock:
            state.daily_next = time.time() + delay
        await interruptible_sleep(state, delay)

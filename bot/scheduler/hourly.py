"""/hourly 領取 loop — 錨定到時鐘整點。"""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Awaitable, Callable

from bot.core.constants import (
    HOURLY_POST_BOUNDARY_MAX_SEC,
    HOURLY_POST_BOUNDARY_MIN_SEC,
)
from bot.core.state import BotState, interruptible_sleep, wait_while_paused
from bot.discord.client import send_and_capture_balance
from bot.notifications.digest import maybe_notify_goal

if TYPE_CHECKING:
    from playwright.async_api import Page

    from bot.core.config import BotConfig

log = logging.getLogger(__name__)


def _seconds_until_next_hour_boundary(now: datetime | None = None) -> float:
    now = now or datetime.now()
    next_hour = (now.replace(minute=0, second=0, microsecond=0)
                 + timedelta(hours=1))
    return max(0.0, (next_hour - now).total_seconds())


async def hourly_loop(
    page: "Page", state: BotState,
    config_provider: Callable[[], "BotConfig"],
    on_config_save: Callable[["BotConfig"], Awaitable[None]],
) -> None:
    """/hourly 領取 — 每個整點 + 隨機 jitter 送一次。"""
    while not state.quit:
        await wait_while_paused(state)
        if state.quit:
            break

        wait_to_boundary = _seconds_until_next_hour_boundary()
        jitter = random.uniform(HOURLY_POST_BOUNDARY_MIN_SEC,
                                HOURLY_POST_BOUNDARY_MAX_SEC)
        delay = wait_to_boundary + jitter
        async with state.lock:
            state.hourly_next = time.time() + delay
        log.info("下次 /hourly 在 %.0f 秒後(過下個整點 +%.0fs jitter)", delay, jitter)
        await interruptible_sleep(state, delay)
        if state.quit:
            break
        await wait_while_paused(state)
        if state.quit:
            break

        new_bal = await send_and_capture_balance(
            page, "/hourly", timeout=20.0, stability_sec=2.0,
        )
        if new_bal is not None:
            async with state.lock:
                state.balance = new_bal
                state.events.hourly_claims += 1
            log.info("/hourly 完成,餘額更新為 %d", new_bal)
            await maybe_notify_goal(page, state, config_provider(), on_config_save)
        else:
            log.info("/hourly 已送出但未取得新餘額,5 分鐘後再試一次")
            await interruptible_sleep(state, 300)
            if state.quit:
                break
            new_bal = await send_and_capture_balance(
                page, "/hourly", timeout=20.0, stability_sec=2.0,
            )
            if new_bal is not None:
                async with state.lock:
                    state.balance = new_bal
                    state.events.hourly_claims += 1
                log.info("/hourly 重試成功,餘額 %d", new_bal)
                await maybe_notify_goal(page, state, config_provider(), on_config_save)
            else:
                log.warning("/hourly 重試仍失敗,跳過此小時")

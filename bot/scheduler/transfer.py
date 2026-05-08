"""自動轉帳 loop。"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from bot.core.constants import DEFAULT_TRANSFER_INTERVAL_MIN
from bot.core.state import BotState, interruptible_sleep, wait_while_paused
from bot.discord.client import do_transfer

if TYPE_CHECKING:
    from playwright.async_api import Page

    from bot.core.config import BotConfig

log = logging.getLogger(__name__)


async def transfer_loop(
    page: "Page", state: BotState,
    config_provider: Callable[[], "BotConfig"],
    on_config_save: Callable[["BotConfig"], Awaitable[None]],   # noqa: ARG001
) -> None:
    while not state.quit:
        await wait_while_paused(state)
        if state.quit:
            break

        tcfg = config_provider().transfer
        if not tcfg.enabled:
            await interruptible_sleep(state, 30)
            continue

        target = (tcfg.target or "").strip()
        amount = int(tcfg.amount or 0)

        if not target or amount <= 0:
            log.warning("自動轉帳設定不完整 (target=%r, amount=%d),30 秒後重試",
                        target, amount)
            await interruptible_sleep(state, 30)
            continue

        try:
            ok = await do_transfer(page, target, amount)
            if ok:
                async with state.lock:
                    state.events.transfers += 1
                state.queue_log(f"💸 已轉帳 {amount:,} → {target}")
            else:
                state.queue_log(f"⚠ 轉帳指令送出但找不到確認按鈕 ({target} {amount:,})")
        except Exception as e:    # noqa: BLE001
            log.exception("自動轉帳發生未預期錯誤")
            state.queue_log(f"⚠ 自動轉帳發生錯誤: {e}")

        try:
            interval = float(tcfg.interval_min)
        except (TypeError, ValueError):
            interval = DEFAULT_TRANSFER_INTERVAL_MIN
        interval = max(1.0, interval)
        await interruptible_sleep(state, interval * 60)

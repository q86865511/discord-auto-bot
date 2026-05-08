"""每日 email 摘要 loop。"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

from bot.core.constants import DEFAULT_DIGEST_HOUR
from bot.core.state import BotState, interruptible_sleep
from bot.notifications.digest import build_digest_body
from bot.notifications.email import send_email

if TYPE_CHECKING:
    from bot.core.config import BotConfig

log = logging.getLogger(__name__)


async def digest_loop(
    state: BotState,
    config_provider: Callable[[], "BotConfig"],
    on_config_save: Callable[["BotConfig"], Awaitable[None]],   # noqa: ARG001
) -> None:
    last_sent_date: str | None = None
    while not state.quit:
        await interruptible_sleep(state, 60)
        if state.quit:
            break

        ecfg = config_provider().email
        if not (ecfg.enabled and ecfg.notify_digest):
            continue

        target_hour = ecfg.digest_hour
        if not 0 <= target_hour <= 23:
            target_hour = DEFAULT_DIGEST_HOUR

        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        if now.hour == target_hour and last_sent_date != today:
            try:
                body = build_digest_body(state, state.slot_analysis or {})
                ok = await send_email(
                    ecfg, f"[Discord Bot] 📊 每日摘要 {today}", body,
                )
                if ok:
                    log.info("每日摘要 email 已寄出")
                    state.queue_log(f"📧 每日摘要已寄出 → {ecfg.to}")
                    last_sent_date = today
                    async with state.lock:
                        state.reset_event_counters()
                else:
                    log.warning("每日摘要 email 寄出失敗")
            except Exception as e:    # noqa: BLE001
                log.exception("digest_loop 例外: %s", e)

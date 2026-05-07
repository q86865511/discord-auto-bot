"""版本檢查 loop — 定期跟 GitHub 比對 commit hash。

行為:
- updater.auto_check=true:每 check_interval_min 分鐘做一次 ls-remote
- 偵測到新版 → 寫 state.update_available + queue_log + (可選)寄 email
- updater.auto_update=true:自動 git pull + 觸發 reboot
- 不會 fetch、不會碰 working tree(除非 auto_update 觸發)
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from bot.core.state import BotState, interruptible_sleep
from bot.core.updater import check_for_updates, perform_update

if TYPE_CHECKING:
    from bot.core.config import BotConfig

log = logging.getLogger(__name__)


async def updater_loop(
    state: BotState,
    config_provider: Callable[[], BotConfig],
    on_config_save: Callable[[BotConfig], Awaitable[None]],   # noqa: ARG001
) -> None:
    # 啟動後 30 秒做第一次檢查(讓 bot 先進入穩定狀態)
    await interruptible_sleep(state, 30)

    while not state.quit:
        ucfg = config_provider().updater
        if not ucfg.auto_check:
            await interruptible_sleep(state, 60)
            continue

        last = state.last_update_check or 0.0
        interval_sec = max(300, int(ucfg.check_interval_min) * 60)
        if time.time() - last < interval_sec:
            await interruptible_sleep(state, 30)
            continue

        async with state.lock:
            state.last_update_check = time.time()

        try:
            status = await check_for_updates(ucfg.branch)
        except Exception:    # noqa: BLE001
            log.exception("版本檢查失敗")
            await interruptible_sleep(state, 60)
            continue

        if status.error:
            log.debug("版本檢查跳過: %s", status.error)
            await interruptible_sleep(state, 60)
            continue

        async with state.lock:
            state.local_commit = status.local_commit
            state.remote_commit = status.remote_commit
            previously = state.update_available
            state.update_available = status.has_update

        if status.has_update:
            local_short  = (status.local_commit  or "")[:7]
            remote_short = (status.remote_commit or "")[:7]
            if not previously:
                msg = f"🔔 GitHub 偵測到新版: {local_short} → {remote_short}"
                log.info(msg)
                state.queue_log(msg)

            if ucfg.auto_update:
                log.info("auto_update=true,執行 git pull...")
                state.queue_log("⬆ 自動更新 — git pull...")
                ok, output = await perform_update(ucfg.branch)
                if ok:
                    log.info("git pull 成功 — 觸發 reboot")
                    state.queue_log("✅ 更新成功,3 秒後重啟程式")
                    await interruptible_sleep(state, 3)
                    async with state.lock:
                        state.reboot = True
                        state.quit = True
                    return
                log.warning("git pull 失敗: %s", output)
                state.queue_log(f"⚠ 自動更新失敗: {output[:80]}")
        else:
            log.debug("版本檢查:已是最新版(local=%s remote=%s)",
                      (status.local_commit or "")[:7],
                      (status.remote_commit or "")[:7])

        await interruptible_sleep(state, 60)

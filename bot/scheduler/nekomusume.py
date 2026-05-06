"""貓娘監控 loop。

啟動 → /check 一次對齊派遣剩餘時間 → 純本地倒數 → 倒數結束 → /check 確認
→ 通知。每次派遣只送 2 次 /check。
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable

from bot.core.constants import (
    DEFAULT_NEKO_INTERVAL_MIN,
    DEFAULT_NOTIFY_USER_ID,
)
from bot.core.state import BotState, interruptible_sleep, wait_while_paused
from bot.discord.client import (
    auto_claim_and_redispatch_neko,
    command_lock,
    parse_dispatch_status,
    read_check_response,
    send_message,
)
from bot.notifications.email import send_email

if TYPE_CHECKING:
    from playwright.async_api import Page

    from bot.core.config import BotConfig

log = logging.getLogger(__name__)


async def nekomusume_loop(
    page: "Page", state: BotState,
    config_provider: Callable[[], "BotConfig"],
    on_config_save: Callable[["BotConfig"], Awaitable[None]],   # noqa: ARG001
) -> None:
    last_status: str | None = None

    async def _query_status() -> tuple[str, int | None]:
        text = await read_check_response(page)
        async with state.lock:
            state.neko_last_check_ts = time.time()
        if text is None:
            return "unknown", None
        return parse_dispatch_status(text)

    async def _notify_completion(*, is_real_transition: bool = True) -> None:
        """派遣完成時:嘗試自動領取 + 通知。

        is_real_transition=True 代表確實偵測到 dispatching → idle 的轉折,
        會增加 events.neko_completes 並寄通知。
        is_real_transition=False(啟動時的 idle 探測)只在「真的領到 loot」
        時才寄通知,沒領到就靜默(可能根本沒有東西能領)。
        """
        ncfg = config_provider().nekomusume
        gcfg = config_provider().gambling
        user_id = (gcfg.notify_user_id or DEFAULT_NOTIFY_USER_ID).strip()

        auto_claimed = False
        if ncfg.auto_claim:
            try:
                async with command_lock:
                    auto_claimed = await auto_claim_and_redispatch_neko(page)
                if auto_claimed:
                    log.info("貓娘已自動領取並再派遣")
                    state.queue_log("🐱 貓娘已自動領取並再派遣")
            except Exception as e:    # noqa: BLE001
                log.exception("自動領取失敗")
                state.queue_log(f"⚠ 貓娘自動領取失敗: {e}")

        # 決定要不要計數 / 寄通知:
        # - 真的轉折 (dispatching → idle):一定算
        # - 啟動探測 (last_status=None → idle):只有真的領到才算
        should_notify = is_real_transition or auto_claimed
        if not should_notify:
            log.debug("啟動探測 — 沒有 loot 可領,不發通知")
            return

        async with state.lock:
            state.events.neko_completes += 1

        try:
            if auto_claimed:
                msg = f"<@{user_id}> 貓娘派遣已完成 — 已自動領取並再派遣 🎉"
            else:
                msg = f"<@{user_id}> 貓娘派遣已完成!記得 `/nekomusume claim` 領取戰利品"
            await send_message(page, msg)
            log.info("已送出貓娘完成通知")
        except Exception as e:    # noqa: BLE001
            log.warning("貓娘完成通知失敗: %s", e)
        ecfg = config_provider().email
        if ecfg.enabled and ecfg.notify_neko:
            body = ("貓娘派遣已完成,已自動領取並再派遣 🎉" if auto_claimed
                    else "貓娘派遣已完成,請至 Discord 用 /nekomusume claim 領取。")
            await send_email(ecfg, "[Discord Bot] 貓娘派遣已完成", body)

    while not state.quit:
        await wait_while_paused(state)
        if state.quit:
            break

        ncfg = config_provider().nekomusume
        if not ncfg.enabled:
            async with state.lock:
                state.neko_deadline_ts = None
            await interruptible_sleep(state, 300)
            continue

        deadline_ts = state.neko_deadline_ts
        now = time.time()

        if deadline_ts is not None and now < deadline_ts:
            await interruptible_sleep(state, deadline_ts - now)
            continue

        new_status, minutes = await _query_status()
        async with state.lock:
            state.neko_status = new_status

        if new_status == "dispatching" and minutes is not None:
            async with state.lock:
                state.neko_deadline_ts = now + minutes * 60
            log.info("貓娘派遣中:剩 %d 分鐘,鎖定本地倒數(不再輪詢)", minutes)
        elif new_status == "dispatching":
            async with state.lock:
                state.neko_deadline_ts = None
            log.info("貓娘派遣中但解析不到時間,稍後重試")
        else:
            async with state.lock:
                state.neko_deadline_ts = None
            if last_status == "dispatching":
                # 確實偵測到完成轉折 — 一定計數 + 通知
                await _notify_completion(is_real_transition=True)
            elif last_status is None:
                # 剛啟動 + 偵測到 idle:可能 cat 閒置在領取狀態
                # (bot 上次沒跑完、或人工已派遣完成但沒領)
                # 試一次 auto_claim;有領到就跟轉折一樣處理,沒領到就靜默
                ncfg2 = config_provider().nekomusume
                if ncfg2.auto_claim:
                    log.info("剛啟動偵測到 idle — 嘗試 auto_claim 探測")
                    await _notify_completion(is_real_transition=False)
                else:
                    log.info("貓娘狀態: %s(閒置)", new_status)
            else:
                log.info("貓娘狀態: %s(閒置)", new_status)

        last_status = new_status

        if state.neko_deadline_ts is None:
            base_min = float(ncfg.check_interval_min or DEFAULT_NEKO_INTERVAL_MIN)
            await interruptible_sleep(state, base_min * 60)

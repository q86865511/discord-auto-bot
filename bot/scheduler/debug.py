"""🐛 除錯訊息頻道 loop — 把 state.debug_pending 中的 WARNING+ 紀錄
推到一個獨立的 Discord 頻道。

用途:user 不在電腦旁時也能在手機 Discord 看到 bot 運行狀態
(transfer 失敗 / stock parser 跳掉 / channel session 過期 等)。
跟 X 鍵除錯紀錄、bot.log 並行,不取代它們。

工作流程:
1. UILogHandler.emit 在 push error_lines 時同時 push 到 state.debug_pending
   (排除 debug_loop 自身,避免 feedback loop)
2. 本 loop 每 poll_interval_sec 醒一次,drain min_level 以上的 entries
   (最多 max_per_flush 筆),組成單則 Discord 訊息送出
3. 用 channel_context 整段持 command_lock,切到 debug channel → send →
   切回主頻道(跟 stock/news/neko 同模式)
4. 送失敗連續 5 次 → auto_paused 冷卻 30 分鐘,避免 spam 無回應指令

跟其他 loop 不同之處:
- 不在 stock snapshot ready 之後啟動;一啟動就準備送(因為錯誤可能在
  startup 階段就發生)
- sleep 時不主動篩 pending(等下一輪 poll 才 drain),簡化邏輯
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from bot.core.state import (
    BotState,
    interruptible_sleep,
    is_loop_auto_paused,
    mark_loop_failed,
    mark_loop_ok,
    mark_loop_running,
    wait_while_paused,
)
from bot.discord.client import channel_context, send_message

if TYPE_CHECKING:
    from playwright.async_api import Page

    from bot.core.config import BotConfig

# 用獨立 logger name(不在 UILogHandler._DEBUG_LOOP_LOGGER 中所列的話會
# 觸發 feedback loop;這個 name 必須跟 main.py 的常數一致)
log = logging.getLogger(__name__)

_LOOP_NAME = "debug"
_PAUSE_RECOVERY_SEC = 30 * 60       # auto_paused 冷卻 30 分鐘
_IDLE_SLEEP_SEC = 30                 # debug 停用時的空轉 sleep

_LEVEL_RANK = {
    "DEBUG":    10,
    "INFO":     20,
    "WARNING":  30,
    "ERROR":    40,
    "CRITICAL": 50,
}


def _level_to_int(name: str) -> int:
    return _LEVEL_RANK.get((name or "").upper(), 30)


_DISCORD_MSG_LIMIT = 1900       # Discord 上限 2000 留 100 緩衝

# 等級對應的 emoji — 一眼分辨嚴重度
_LEVEL_EMOJI = {
    "WARNING":  "⚠️",
    "ERROR":    "❌",
    "CRITICAL": "🚨",
}


def _format_messages(entries: list[dict], include_logger_name: bool) -> str:
    """把待送 entries 組成 Discord 訊息(單則,markdown 格式)。

    範例輸出:
        **Debug** · 1 筆 · 2026-05-11
        ⚠️ `20:32:44` **scheduler.gambling** — 連敗 5 場 ≥5,暫停下注 5.0 分鐘

    設計考量:
    - 標題不放 emoji 開頭 — 避免 Discord 把「emoji 開頭單行訊息」渲染成
      巨大 emoji 圖示破壞排版
    - 用 markdown bold / inline code 區隔欄位,易讀
    - 各 level 配色 emoji(⚠️ / ❌ / 🚨)讓嚴重度一眼分辨
    - 加當天日期 — 手機推播只看到第一行時也有時間 context
    - 每筆 msg 截 200 字,總長超 _DISCORD_MSG_LIMIT 在 entry 邊界截斷
      (不切到一半的訊息),尾巴加「(+N 筆截斷)」註記
    """
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    header = f"**Debug** · {len(entries)} 筆 · {today}"
    lines = [header]
    total_len = len(header)
    truncated = 0
    for i, e in enumerate(entries):
        ts = e.get("ts", "??:??:??")
        lv = (e.get("level") or "INFO").upper()
        emoji = _LEVEL_EMOJI.get(lv, "⚠️")
        msg = (e.get("msg") or "").replace("\n", " ").replace("\r", " ")
        if len(msg) > 200:
            msg = msg[:197] + "..."
        if include_logger_name:
            lg = e.get("logger", "?")
            # 去掉冗長前綴 "bot." 讓訊息更精簡
            if lg.startswith("bot."):
                lg = lg[4:]
            line = f"{emoji} `{ts}` **{lg}** — {msg}"
        else:
            line = f"{emoji} `{ts}` — {msg}"
        # 預估加上此行後的長度(含 newline)
        if total_len + 1 + len(line) > _DISCORD_MSG_LIMIT:
            truncated = len(entries) - i
            break
        lines.append(line)
        total_len += 1 + len(line)
    if truncated > 0:
        tail = f"_…(+{truncated} 筆截斷,下輪繼續送)_"
        if total_len + 1 + len(tail) <= _DISCORD_MSG_LIMIT:
            lines.append(tail)
    return "\n".join(lines)


def _drain_pending(
    state: BotState, min_level: str, max_per_flush: int,
) -> list[dict]:
    """從 state.debug_pending 取出最多 max_per_flush 筆 level >= min_level 的
    entries。低於 min_level 的也會 drain 掉(否則永遠卡在 queue 前頭)。

    呼叫端不需持 state.lock — deque 的 popleft 是 thread-safe(GIL 保護)。
    """
    min_int = _level_to_int(min_level)
    out: list[dict] = []
    while state.debug_pending and len(out) < max_per_flush:
        try:
            item = state.debug_pending.popleft()
        except IndexError:
            break
        if _level_to_int(item.get("level", "INFO")) >= min_int:
            out.append(item)
        # 低於門檻的直接丟,不重排隊
    return out


async def debug_loop(
    page: "Page", state: BotState,
    config_provider: Callable[[], "BotConfig"],
    on_config_save: Callable[["BotConfig"], Awaitable[None]],   # noqa: ARG001
) -> None:
    """除錯訊息頻道 push loop。"""
    # 啟動延遲:讓其他 loop 先初始化,避免 startup phase 一堆 INFO/WARNING
    # 雜訊馬上被 push 到 Discord(尤其貓娘 / 股票首次 poll 的 warning)
    await interruptible_sleep(state, 30)

    # Startup 期間(UILogHandler 已在運作但 debug_loop 還沒醒)累積的
    # pending 一律丟棄,避免使用者啟用後 Discord 立刻被 startup 噪音洗版。
    # 30 秒後第一次進迴圈時 fresh start。
    if state.debug_pending:
        n = len(state.debug_pending)
        state.debug_pending.clear()
        log.info("debug loop: 啟動丟棄 startup 期間累積的 %d 筆 WARNING+", n)

    while not state.quit:
        await wait_while_paused(state)
        if state.quit:
            break

        cfg = config_provider()
        dcfg = cfg.debug

        # 停用 / 沒設 channel → 空轉(不要拋錯,避免 user 沒設就一直 WARN)
        if not dcfg.enabled or not (dcfg.channel_id or "").strip():
            # 同時清空 pending,避免 user 之後啟用時被累積的訊息洗版
            if state.debug_pending:
                state.debug_pending.clear()
            await interruptible_sleep(state, _IDLE_SLEEP_SEC)
            continue

        # auto_paused 冷卻
        if is_loop_auto_paused(state, _LOOP_NAME):
            log.warning("debug: 連續失敗已達閾值,冷卻 %d 秒後重試",
                        _PAUSE_RECOVERY_SEC)
            state.queue_log("⛔ 除錯頻道連續送失敗,已自動暫停 30 分鐘")
            await interruptible_sleep(state, _PAUSE_RECOVERY_SEC)
            if state.quit:
                break
            # 冷卻結束:reset fail_streak 給乾淨一次嘗試。否則 fail_streak
            # 還是 5,下次再失敗 1 次又進冷卻 → 永遠回不來
            h = state.loop_health.get(_LOOP_NAME)
            if h is not None:
                h["fail_streak"] = 0
                h["status"] = "idle"

        # Drain pending — 沒東西就空轉一段(沒 WARNING 不用送,正常情況)
        try:
            max_per = max(1, int(dcfg.max_per_flush or 5))
        except (TypeError, ValueError):
            max_per = 5
        entries = _drain_pending(state, dcfg.min_level or "WARNING", max_per)
        if not entries:
            # idle 也算「loop 健康」— mark_loop_ok 讓 UI footer 顯示 ✓
            mark_loop_ok(state, _LOOP_NAME)
            try:
                poll_sec = max(10, float(dcfg.poll_interval_sec or 60))
            except (TypeError, ValueError):
                poll_sec = 60.0
            await interruptible_sleep(state, poll_sec)
            continue

        # 有東西要送 → 切到 debug channel,送出
        msg_text = _format_messages(
            entries, include_logger_name=bool(dcfg.include_logger_name),
        )
        mark_loop_running(state, _LOOP_NAME)
        try:
            async with channel_context(page, state, dcfg.channel_id):
                await send_message(page, msg_text, acquire_lock=False)
            mark_loop_ok(state, _LOOP_NAME)
        except Exception as e:    # noqa: BLE001
            log.exception("debug loop: 送 Discord debug channel 失敗")
            mark_loop_failed(state, _LOOP_NAME, str(e))
            # 重要:失敗時不要把 entries 加回 pending,避免 retry spam
            # (如果頻道永久壞了會無限重送)。auto_paused 機制會接手。

        # 送完後仍 sleep poll_interval_sec,避免短時間連續送多次
        try:
            poll_sec = max(10, float(dcfg.poll_interval_sec or 60))
        except (TypeError, ValueError):
            poll_sec = 60.0
        await interruptible_sleep(state, poll_sec)

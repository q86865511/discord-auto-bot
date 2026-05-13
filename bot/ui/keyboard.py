"""鍵盤監聽 — 背景 thread 把按鍵推進 state.key_queue。

只在 Windows 用(msvcrt);其他平台目前用不到所以沒做 fallback。

⚠️ 特殊鍵 prefix 處理 — 修「按方向鍵會誤觸 P/Q/F 鍵」根因 bug:

Windows msvcrt.getch() 對方向鍵 / F 鍵 / Home/End/PgUp/PgDn 等「特殊鍵」會
回**兩個 bytes**:第一個是 prefix(`\\x00` 或 `\\xe0`),第二個才是實際
scan code。原本邏輯把第一 byte decode UTF-8 失敗就吞掉,但第二 byte 仍會
被讀進 queue 當成獨立按鍵 — 造成:

    ⬇ Down  → 第二 byte 是 'P' → 解成 'p' → **誤觸暫停**
    ⬅ Left  → 'K' → **誤觸 K 鍵 QR**
    PgDn    → 'Q' → **誤觸 Q 鍵退出整支 bot**
    Del     → 'S' → **誤觸拉霸分析(live.stop 阻塞)**
    F11     → 'E' / 後續 byte 也可能映到 'F' → **誤觸 F 重啟**

修法:偵測到 prefix byte 就主動把第二 byte 也讀掉但不 push,讓特殊鍵變成
no-op。
"""
from __future__ import annotations

import logging
import msvcrt
import threading
import time

from bot.core.state import BotState

log = logging.getLogger(__name__)

# Windows 特殊鍵 prefix(getch 兩階段讀取的第一個 byte)
_SPECIAL_KEY_PREFIXES = (b"\x00", b"\xe0")


def start_kb_listener(state: BotState) -> None:
    def _listen() -> None:
        while not state.quit:
            try:
                if msvcrt.kbhit():
                    raw = msvcrt.getch()
                    # 特殊鍵第一 byte → 吃掉第二 byte 後跳過,不 push
                    if raw in _SPECIAL_KEY_PREFIXES:
                        # 第二 byte 通常 buffer 中立即可讀
                        if msvcrt.kbhit():
                            msvcrt.getch()
                        else:
                            # 萬一還沒 ready,等 10ms 再讀一次
                            time.sleep(0.01)
                            if msvcrt.kbhit():
                                msvcrt.getch()
                        continue
                    try:
                        key = raw.decode("utf-8").lower()
                        state.push_key(key)
                    except UnicodeDecodeError:
                        pass
            except OSError as e:
                log.debug("kb listener OSError: %s", e)
            time.sleep(0.05)
    threading.Thread(target=_listen, daemon=True, name="kb-listener").start()

"""鍵盤監聽 — 背景 thread 把按鍵推進 state.key_queue。

只在 Windows 用(msvcrt);其他平台目前用不到所以沒做 fallback。
"""
from __future__ import annotations

import logging
import msvcrt
import threading
import time

from bot.core.state import BotState

log = logging.getLogger(__name__)


def start_kb_listener(state: BotState) -> None:
    def _listen() -> None:
        while not state.quit:
            try:
                if msvcrt.kbhit():
                    raw = msvcrt.getch()
                    try:
                        key = raw.decode("utf-8").lower()
                        state.push_key(key)
                    except UnicodeDecodeError:
                        pass
            except OSError as e:
                log.debug("kb listener OSError: %s", e)
            time.sleep(0.05)
    threading.Thread(target=_listen, daemon=True, name="kb-listener").start()

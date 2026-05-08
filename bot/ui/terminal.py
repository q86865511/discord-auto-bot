"""終端 Rich UI 主迴圈。

實際內容拆到 sibling modules:
    bot.ui.keyboard       msvcrt 鍵盤 listener
    bot.ui.layout         Rich Live layout 組裝
    bot.ui.exports        E 鍵 — CSV / 圖表 / Slot 分析報告
    bot.ui.analysis_view  S 鍵 — 終端內顯示拉霸分析
    bot.ui.stock_view     T 鍵 — 終端內顯示股票分析

terminal.py 本身只剩 ui_loop。為了向後相容,常用符號直接 re-export。
"""
from __future__ import annotations

import asyncio
import logging
import webbrowser
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live

from bot.core.state import BotState
from bot.ui.analysis_view import show_slot_analysis
from bot.ui.exports import (
    export_history_chart,
    export_history_csv,
    export_slot_analysis,
)
from bot.ui.keyboard import start_kb_listener
from bot.ui.layout import build_layout, fmt_remaining
from bot.ui.stock_view import show_stock_analysis

if TYPE_CHECKING:
    from bot.core.config import BotConfig

log = logging.getLogger(__name__)
console = Console()

__all__ = [
    "build_layout",
    "export_history_chart",
    "export_history_csv",
    "export_slot_analysis",
    "fmt_remaining",
    "show_slot_analysis",
    "show_stock_analysis",
    "start_kb_listener",
    "ui_loop",
]


async def ui_loop(
    state: BotState,
    config_provider: Callable[[], BotConfig],
    on_menu: Callable[[], Awaitable[None]],
    on_qr_open: Callable[[], Awaitable[None]],
    on_export: Callable[[], Awaitable[None]],
) -> None:
    with Live(
        build_layout(state, config_provider()),
        console=console, refresh_per_second=2, screen=True,
    ) as live:
        while not state.quit:
            key = state.pop_key()
            if key:
                if key == "q":
                    async with state.lock:
                        state.quit = True
                    break
                elif key == "c":
                    live.stop()
                    try:
                        await on_menu()
                    finally:
                        live.start()
                elif key == "p":
                    async with state.lock:
                        state.paused = not state.paused
                    state.queue_log("已暫停所有功能(再按 P 恢復)"
                                    if state.paused else "已恢復運行")
                elif key == "e":
                    await on_export()
                elif key == "s":
                    live.stop()
                    try:
                        show_slot_analysis(state)
                    finally:
                        live.start()
                elif key == "t":
                    live.stop()
                    try:
                        show_stock_analysis(state)
                    finally:
                        live.start()
                elif key == "f":
                    state.queue_log("已請求重啟,正在收尾...")
                    async with state.lock:
                        state.reboot = True
                        state.quit = True
                    break
                elif key == "w":
                    from bot.web.url_helpers import dashboard_local_url
                    url = dashboard_local_url(config_provider())
                    try:
                        webbrowser.open(url)
                        state.queue_log(f"🌐 已在瀏覽器打開 {url}")
                    except Exception as e:    # noqa: BLE001
                        state.queue_log(f"⚠ 開啟瀏覽器失敗: {e}")
                elif key == "k":
                    await on_qr_open()

            live.update(build_layout(state, config_provider()))
            await asyncio.sleep(0.5)

"""X 鍵 — 全螢幕顯示最近錯誤紀錄(WARNING+)。

呼叫端先 live.stop(),然後 show_error_log(state),等使用者按 Enter 後再
live.start()。資料來源:state.error_lines(由 UILogHandler 在 WARNING+ 時 push)。
"""
from __future__ import annotations

import os

from rich.console import Console
from rich.table import Table

from bot.core.state import BotState

console = Console()


def show_error_log(state: BotState) -> None:
    """同步顯示除錯紀錄頁面。呼叫端應 live.stop() 後再呼叫。"""
    os.system("cls")
    errs = list(state.error_lines) if state.error_lines else []
    console.print()
    console.rule(f"[bold red]🐛 除錯紀錄 — {len(errs)} 筆 (WARNING+)[/]")
    console.print()

    if not errs:
        console.print("  [green]✓ 目前沒有任何錯誤紀錄[/green]")
        console.print()
        console.print("  [dim]這裡會收集 WARNING / ERROR / EXCEPTION 等級的 log。"
                      "loop 失敗、Discord 沒回應、parse 抓不到 — 都會出現在這。[/dim]")
    else:
        t = Table(show_header=True, header_style="bold red", expand=True)
        t.add_column("時間",   style="dim",  width=10, no_wrap=True)
        t.add_column("等級",   width=9,      no_wrap=True)
        t.add_column("Logger", style="cyan", width=32, no_wrap=True)
        t.add_column("訊息",   overflow="fold")
        # 最新在上(error_lines 是按 push 順序的 deque)
        for e in reversed(errs):
            lvl = e.get("level", "?")
            if lvl == "ERROR" or lvl == "CRITICAL":
                lvl_disp = f"[bold red]{lvl}[/bold red]"
            elif lvl == "WARNING":
                lvl_disp = f"[yellow]{lvl}[/yellow]"
            else:
                lvl_disp = f"[white]{lvl}[/white]"
            t.add_row(
                e.get("ts", "—"),
                lvl_disp,
                (e.get("logger") or "—")[:32],
                (e.get("msg") or "")[:300],
            )
        console.print(t)

    console.print()
    console.print("  [dim]X 鍵或 Enter 返回主畫面[/dim]")
    input("\n  按 Enter 返回...")

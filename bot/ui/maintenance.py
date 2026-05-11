"""進階選單 — 檔案管理(刪除 logs / exports)+ 系統更新(git pull)。

由 menu.py 的「[7] 進階」項目進入。獨立成檔讓 menu 主檔聚焦在 config 編輯。
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from bot.core.constants import (
    EXPORT_DIR,
    LOG_FILE_BACKUP_COUNT,
    LOG_FILE_PATH,
    SLOT_DEBUG_LOG_PATH,
)
from bot.core.state import BotState
from bot.slot.analysis import make_slot_analysis
from bot.ui.input_validation import (
    ainput,
    ask_yes_no,
    wait_enter,
)

if TYPE_CHECKING:
    from bot.core.db import Database

log = logging.getLogger(__name__)


def _file_size_str(path: str) -> str:
    if not os.path.exists(path):
        return "—"
    try:
        size = os.path.getsize(path)
    except OSError:
        return "—"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 / 1024:.2f} MB"


def _dir_size_str(path: str) -> tuple[str, int]:
    if not os.path.isdir(path):
        return "—", 0
    total = 0
    count = 0
    try:
        for root, _, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                    count += 1
                except OSError:
                    pass
    except OSError:
        return "—", 0
    if total < 1024:
        sz = f"{total} B"
    elif total < 1024 * 1024:
        sz = f"{total / 1024:.1f} KB"
    else:
        sz = f"{total / 1024 / 1024:.2f} MB"
    return sz, count


def _delete_dir_contents_sync(path: str) -> tuple[int, int]:
    if not os.path.isdir(path):
        return 0, 0
    deleted = 0
    failed = 0
    for entry in os.listdir(path):
        fp = os.path.join(path, entry)
        if os.path.isfile(fp):
            try:
                os.remove(fp)
                deleted += 1
            except OSError as e:
                print(f"  ⚠ 刪除失敗 {fp}: {e}")
                failed += 1
    return deleted, failed


def _clear_logs() -> None:
    import logging.handlers
    root = logging.getLogger()
    file_handlers = [
        h for h in root.handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    for h in file_handlers:
        h.close()
        root.removeHandler(h)
    cleared = 0
    paths = [LOG_FILE_PATH] + [f"{LOG_FILE_PATH}.{i}" for i in range(1, LOG_FILE_BACKUP_COUNT + 1)]
    for p in paths:
        if os.path.exists(p):
            try:
                os.remove(p)
                cleared += 1
            except OSError as e:
                print(f"  ⚠ 刪除失敗 {p}: {e}")
    # 重建 handler
    from bot.core.constants import LOG_FILE_MAX_BYTES
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE_PATH, maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUP_COUNT, encoding="utf-8",
        )
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(fh)
    except OSError as e:
        print(f"  ⚠ 重建 file handler 失敗: {e}")
    print(f"  ✓ 清空 {cleared} 個 log 檔案")


async def _do_system_update(state: BotState) -> bool:
    from bot.core.async_io import run_subprocess
    print("\n  正在執行 git pull origin main ...")
    rc, out, err = await run_subprocess(["git", "pull", "--ff-only", "origin", "main"], timeout=60)
    output = (out or "") + (err or "")
    print("  --- git output ---")
    for line in output.strip().splitlines()[-15:]:
        print(f"  {line}")
    print("  ------------------")

    if rc != 0:
        print(f"  ⚠ git pull 失敗(exit code {rc})")
        return False
    if "Already up to date" in output or "Already up-to-date" in output:
        print("  ✓ 已是最新版本")
        return False

    print("  ✓ 更新成功!3 秒後重啟程式...")
    import asyncio
    await asyncio.sleep(3)
    async with state.lock:
        state.reboot = True
        state.quit = True
    return True


async def run_advanced_menu(state: BotState, db: Database) -> None:
    while True:
        os.system("cls")
        print(f"\n{'═'*48}\n  🛠️  進階設定\n{'═'*48}")

        debug_size = _file_size_str(SLOT_DEBUG_LOG_PATH)
        bot_log_size = _file_size_str(LOG_FILE_PATH)
        exports_size, exports_count = _dir_size_str(EXPORT_DIR)
        sa_spins = (state.slot_analysis or {}).get("total_spins", 0)
        history_count = len(state.history or [])
        # 股票新聞筆數(DB query;失敗不影響選單)
        try:
            _news_rows = await db.load_recent_news(limit=100000)
            news_count = len(_news_rows)
        except Exception:    # noqa: BLE001
            news_count = 0

        print("  [檔案管理]")
        print(f"   [1] 刪除 slot_debug.log              ({debug_size})")
        print(f"   [2] 刪除 exports/ 內所有檔案         ({exports_count} 檔, {exports_size})")
        print(f"   [3] 清空下注歷史(DB)                ({history_count} 筆)")
        print(f"   [4] 重置 slot 分析(DB)              ({sa_spins} 筆)")
        print(f"   [9] 清空股票新聞(DB)                ({news_count} 筆)")
        print("   [5] 一鍵清除以上全部")
        print()
        print("  [日誌]")
        print(f"   [7] 開啟 bot.log                     ({bot_log_size})")
        print("   [8] 清空 bot.log + 輪替檔")
        print()
        print("  [系統]")
        print("   [6] 系統更新(git pull + 重啟)")
        print()
        print("  [0] 返回上一頁")

        choice = (await ainput("\n  選擇: ")).strip()
        if choice == "0":
            break
        elif choice == "1":
            if await ask_yes_no("確認刪除 slot_debug.log?"):
                from bot.core.async_io import remove_file
                if await remove_file(SLOT_DEBUG_LOG_PATH):
                    print("  ✓ 已刪除")
                else:
                    print("  (檔案不存在或刪除失敗)")
                await wait_enter()
        elif choice == "2":
            if await ask_yes_no(f"確認刪除 exports/ 內 {exports_count} 個檔案?"):
                d_count, f_count = _delete_dir_contents_sync(EXPORT_DIR)
                print(f"  ✓ 刪除 {d_count} 個檔案" + (f",{f_count} 個失敗" if f_count else ""))
                await wait_enter()
        elif choice == "3":
            if await ask_yes_no("確認清空所有下注歷史?(DB)"):
                await db.clear_history()
                async with state.lock:
                    state.history = []
                print("  ✓ 已清空")
                await wait_enter()
        elif choice == "4":
            if await ask_yes_no(f"確認重置 slot 分析?({sa_spins} 筆紀錄)"):
                async with state.lock:
                    state.slot_analysis = make_slot_analysis()
                await db.reset_slot_analysis()
                print("  ✓ 已重置")
                await wait_enter()
        elif choice == "5":
            if await ask_yes_no("⚠ 確認刪除以上所有檔案+重置分析+清空新聞?"):
                from bot.core.async_io import remove_file
                await remove_file(SLOT_DEBUG_LOG_PATH)
                d_count, _ = _delete_dir_contents_sync(EXPORT_DIR)
                await db.clear_history()
                await db.reset_slot_analysis()
                n_news = await db.clear_all_news()
                async with state.lock:
                    state.history = []
                    state.slot_analysis = make_slot_analysis()
                    state.stock_recent_news = []
                    state.news_force_poll = True
                print(f"  ✓ 已清除全部(exports {d_count} 檔,新聞 {n_news} 筆)")
                await wait_enter()
        elif choice == "6":
            if await ask_yes_no("將執行 git pull origin main 並可能重啟。確定?"):
                rebooted = await _do_system_update(state)
                if rebooted:
                    return
                await wait_enter()
        elif choice == "7":
            if os.path.exists(LOG_FILE_PATH):
                try:
                    os.startfile(LOG_FILE_PATH)
                    print(f"  ✓ 已開啟 {LOG_FILE_PATH}")
                except OSError as e:
                    print(f"  ⚠ 無法開啟: {e}")
            else:
                print(f"  ({LOG_FILE_PATH} 不存在)")
            await wait_enter()
        elif choice == "8":
            if await ask_yes_no("確認清空 bot.log 與所有輪替檔?"):
                _clear_logs()
                await wait_enter()
        elif choice == "9":
            print()
            print("  ⚠ 清空 stock_news 資料庫")
            print("  ─────────────────────")
            print("  將刪除所有已抓到的新聞資料(無法復原)。")
            print("  下次 news_loop 會 fresh fetch 全部公司新聞。")
            print()
            print("  通常用於:")
            print("    • DB 中累積了 parser bug 早期的污染 row")
            print("    • 想重抓最新新聞看內容是否正確")
            print()
            if await ask_yes_no("確定清空?"):
                n = await db.clear_all_news()
                async with state.lock:
                    state.stock_recent_news = []
                    state.news_force_poll = True
                print(f"  ✓ 已刪除 {n} 則新聞記錄")
                print("  ✓ 已請求 news_loop 立即重抓(30 秒內 cycle 開始)")
            else:
                print("  取消")
            await wait_enter()

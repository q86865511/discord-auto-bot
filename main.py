"""Discord 自動指令腳本 — 入口檔(精簡版)。

主要邏輯都拆到 bot/ 下的子模組:
- bot/core/      DB / 加密 / 設定 / 狀態 / 日誌 / async I/O
- bot/discord/   Playwright wrappers
- bot/scheduler/ 5 個排程 loop
- bot/notifications/ email + 通知判斷
- bot/slot/      slot embed 解析 + 累計分析
- bot/ui/        Rich UI + 互動式設定選單
- bot/web/       Web dashboard
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys
import tempfile

from playwright.async_api import async_playwright

from bot.core.async_io import remove_file
from bot.core.constants import (
    DB_PATH,
    LEGACY_ANALYSIS_PATH,
    LEGACY_CONFIG_PATH,
    LEGACY_HISTORY_PATH,
    LOG_FILE_BACKUP_COUNT,
    LOG_FILE_MAX_BYTES,
    LOG_FILE_PATH,
    REBOOT_EXIT_CODE,
    STORAGE_STATE_PATH,
)
from bot.core.config import (
    BotConfig,
    load_config,
    merge_partial,
    migrate_analysis_from_json,
    migrate_from_json_if_needed,
    migrate_history_from_json,
    save_config,
)
from bot.core.crypto import Cipher, load_or_create_key
from bot.core.db import init_db
from bot.core.log_filter import RedactingFormatter
from bot.core.state import BotState
from bot.discord.client import navigate_to_channel
from bot.scheduler.daily import daily_loop
from bot.scheduler.digest import digest_loop
from bot.scheduler.gambling import gambling_loop
from bot.scheduler.hourly import hourly_loop
from bot.scheduler.nekomusume import nekomusume_loop
from bot.scheduler.transfer import transfer_loop
from bot.slot.analysis import make_slot_analysis, migrate_slot_analysis
from bot.ui.menu import first_run_wizard, run_config_menu
from bot.ui.terminal import (
    export_history_chart,
    export_history_csv,
    export_slot_analysis,
    start_kb_listener,
    ui_loop,
)


# ── Logging ──────────────────────────────────────────────────────────
class UILogHandler(logging.Handler):
    """把 INFO+ 的 log 推到 state.log_lines 給 UI 顯示。"""

    def __init__(self, state: BotState):
        super().__init__()
        self.state = state
        self.setFormatter(RedactingFormatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        try:
            self.state.log_lines.append(self.format(record))
            # log_lines 是 deque(maxlen=UI_LOG_LINES_MAX),自動 trim
        except (TypeError, ValueError) as e:
            self.handleError(record)


def setup_logging(state: BotState, log_level: str = "INFO") -> logging.Logger:
    level_name = (log_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)

    ui = UILogHandler(state)
    ui.setLevel(logging.INFO)
    root.addHandler(ui)

    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE_PATH,
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.setFormatter(RedactingFormatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(fh)
    except OSError as e:
        print(f"⚠ 無法開啟 {LOG_FILE_PATH}({e}),只用 UI 日誌")

    for noisy in ("urllib3", "asyncio", "playwright", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger(__name__)


# ── 登入精靈(沒 storage_state.json 時引導使用者) ─────────────────
async def _run_login_wizard() -> None:
    print()
    print("=" * 64)
    print("  🔐 Discord 登入 — 找不到或 storage_state.json 已過期")
    print("=" * 64)
    print()
    print("  即將開啟 Chromium 視窗,請手動完成 Discord 登入(含 2FA)。")
    print("  網址跳轉到 /channels/... 時會自動關閉並儲存登入狀態。")
    print("  (登入逾時 5 分鐘)")
    print()
    input("  按 Enter 繼續...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://discord.com/login")
        try:
            await page.wait_for_url("**/channels/**", timeout=300_000)
            print("\n  ✓ 登入成功!儲存 session 中...")
            await context.storage_state(path=STORAGE_STATE_PATH)
            print(f"  ✓ 已儲存至 {STORAGE_STATE_PATH}")
        finally:
            await browser.close()


# ── QR / Dashboard helper(讓 ui_loop 用) ────────────────────────
def _save_qr_png(text: str) -> str | None:
    try:
        import qrcode
        img = qrcode.make(text, box_size=10, border=4)
        path = os.path.join(tempfile.gettempdir(), "discord_bot_qr.png")
        img.save(path)
        return path
    except ImportError:
        return None
    except (OSError, ValueError):
        return None


# ── 主程式 ────────────────────────────────────────────────────────────
async def main() -> None:
    # 1) 初始化加密與 DB
    key = load_or_create_key()
    cipher = Cipher(key)
    db = await init_db(cipher=cipher, path=DB_PATH)

    # 2) 一次性遷移舊 JSON 設定 / 分析 / 歷史(若存在且 DB 沒記錄過遷移)
    migrated_cfg = await migrate_from_json_if_needed(db, LEGACY_CONFIG_PATH)
    migrated_history = await migrate_history_from_json(db, LEGACY_HISTORY_PATH)
    migrated_analysis = await migrate_analysis_from_json(db, LEGACY_ANALYSIS_PATH)
    if migrated_cfg or migrated_history or migrated_analysis:
        print(
            f"  ✓ 已遷移舊資料至 {DB_PATH} "
            f"(config={migrated_cfg}, history={migrated_history}, analysis={migrated_analysis})"
        )

    # 3) 載入 config(必填欄位缺失走 wizard 補)
    config = await load_config(db)
    await first_run_wizard(config)
    # 儲存 wizard 修改的內容
    await save_config(db, config)

    # 4) 沒登入過就跑登入精靈
    if not os.path.exists(STORAGE_STATE_PATH):
        await _run_login_wizard()
        if not os.path.exists(STORAGE_STATE_PATH):
            print("\n⚠ 登入流程未完成,程式中止。")
            return

    # 5) 初始化 state + logging
    state = BotState()
    log = setup_logging(state, log_level=config.log_level)

    if not config.guild_id or not config.channel_id:
        print("\n⚠ guild_id / channel_id 未設定,程式中止。請按 C 進入設定後重啟。")
        return

    state.guild_id = config.guild_id
    state.channel_id = config.channel_id

    # 6) 載入 slot_analysis / history(從 DB)
    sa = await db.load_slot_analysis()
    state.slot_analysis = migrate_slot_analysis(sa) if sa else make_slot_analysis()
    history = await db.load_history()
    state.history = list(history)
    if state.slot_analysis.get("total_spins", 0) > 0:
        log.info("已載入 slot 分析(%d 筆)", state.slot_analysis["total_spins"])
    if state.history:
        log.info("已載入下注歷史(%d 筆)", len(state.history))

    # 7) 啟動鍵盤監聽
    start_kb_listener(state)

    # 8) 啟動 Playwright + 導航
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=STORAGE_STATE_PATH)
        page = await context.new_page()

        try:
            await navigate_to_channel(page, config.guild_id, config.channel_id)
        except RuntimeError as e:
            await browser.close()
            if "session 已過期" in str(e):
                if os.path.exists(STORAGE_STATE_PATH):
                    await remove_file(STORAGE_STATE_PATH)
                await _run_login_wizard()
                print("\n  ✓ 重新登入完成,3 秒後自動重啟 bot...")
                await asyncio.sleep(3)
                sys.exit(REBOOT_EXIT_CODE)
            raise

        # 9) 讀初始餘額
        from bot.discord.client import get_balance, read_initial_balance_from_history
        log.info("等待聊天歷史載入...")
        await asyncio.sleep(3)
        log.info("送 /balance 查詢初始餘額")
        balance = await get_balance(page)
        if balance is not None:
            log.info("從 /balance 取得餘額: %d 油幣", balance)
        else:
            log.warning("/balance 沒回應,回退到從聊天歷史搜尋")
            balance = await read_initial_balance_from_history(page)
            if balance is not None:
                log.info("從歷史訊息讀到餘額: %d 油幣", balance)
            else:
                log.warning("初始餘額讀取失敗,gambling_loop 會自動重試")

        async with state.lock:
            state.balance = balance
            state.start_balance = balance
            state.status = "運行中"

        # 10) Dashboard
        config_holder = [config]   # 共用引用,讓 dashboard / scheduler 都能拿到最新 config
        loop = asyncio.get_running_loop()

        def config_provider() -> BotConfig:
            return config_holder[0]

        async def on_config_save_async(cfg: BotConfig) -> None:
            await save_config(db, cfg)
            config_holder[0] = cfg

        def on_action(action: str) -> dict:
            """Dashboard 按鈕觸發的動作。在 dashboard thread 執行。

            注意:state.paused / state.reboot 等 bool / 物件參考的單一賦值在
            CPython 因 GIL 而是 atomic,故不需 lock。但需要 await 的操作
            (例如 db.reset_slot_analysis)走 run_coroutine_threadsafe。
            """
            try:
                if action == "toggle_pause":
                    state.paused = not state.paused
                    msg = "已暫停" if state.paused else "已恢復"
                    state.queue_log(f"📡 Dashboard:{msg}")
                    return {"ok": True, "message": msg}
                if action == "reset_analysis":
                    n = (state.slot_analysis or {}).get("total_spins", 0)
                    state.slot_analysis = make_slot_analysis()
                    fut = asyncio.run_coroutine_threadsafe(db.reset_slot_analysis(), loop)
                    try:
                        fut.result(timeout=5)
                    except Exception:    # noqa: BLE001
                        log.exception("reset_slot_analysis 失敗")
                    state.queue_log(f"📡 Dashboard:重置分析({n} 筆清除)")
                    return {"ok": True, "message": f"已重置({n} 筆清除)"}
                if action == "restart":
                    state.reboot = True
                    state.quit = True
                    state.queue_log("📡 Dashboard:請求重啟")
                    return {"ok": True, "message": "重啟請求已送出"}
                return {"ok": False, "message": f"未知動作: {action}"}
            except Exception as e:    # noqa: BLE001
                log.exception("on_action 失敗")
                return {"ok": False, "message": f"錯誤: {e}"}

        def on_config_save_sync(partial: dict) -> dict:
            """Dashboard thread → main loop 的 sync 包裝。

            把 partial config 套到目前 config,跑 schema 驗證,然後 schedule
            一個 coroutine 到 main loop 把它存到 DB。所有 thread/coroutine
            的協調都在這裡集中處理。
            """
            try:
                cur = config_holder[0]
                errs = merge_partial(cur, partial)
                if errs:
                    return {"ok": False, "errors": errs, "message": "驗證失敗"}
                fut = asyncio.run_coroutine_threadsafe(save_config(db, cur), loop)
                warnings = fut.result(timeout=5) or []
                config_holder[0] = cur
                return {"ok": True, "warnings": warnings}
            except Exception as e:    # noqa: BLE001
                log.exception("on_config_save_sync 失敗")
                return {"ok": False, "message": f"錯誤: {e}"}

        dashboard_thread = None
        if config.dashboard.enabled:
            try:
                from bot.web.dashboard import start_dashboard_thread
                dashboard_thread = start_dashboard_thread(
                    state, config_provider, on_action, on_config_save_sync,
                )
            except Exception:    # noqa: BLE001
                log.exception("dashboard 啟動失敗")

        # 11) UI hooks
        async def on_menu_open() -> None:
            await run_config_menu(config_holder[0], state, db, on_config_save_async)

        async def on_qr_open() -> None:
            from bot.web.url_helpers import dashboard_lan_url
            url = dashboard_lan_url(config_holder[0])
            path = _save_qr_png(url)
            if path is None:
                state.queue_log("⚠ 產 QR 失敗(請執行 pip install qrcode pillow)")
            else:
                try:
                    os.startfile(path)
                    state.queue_log(f"📱 QR 已開啟: {path} ({url})")
                except OSError as e:
                    state.queue_log(f"⚠ 無法開啟 QR 圖: {e}(手動開啟 {path})")

        async def on_export() -> None:
            csv_path = await export_history_csv(state)
            png_path = await export_history_chart(state)
            analysis_path = await export_slot_analysis(state)
            if csv_path is None and analysis_path is None:
                state.queue_log("尚無賭博紀錄可匯出")
                return
            if csv_path:
                state.queue_log(f"CSV 已匯出: {csv_path}")
            if png_path:
                state.queue_log(f"圖表已匯出: {png_path}")
            elif csv_path:
                state.queue_log("(未安裝 matplotlib,跳過圖表)")
            if analysis_path:
                state.queue_log(f"分析已匯出: {analysis_path}")

        # 12) 啟動所有 task
        ui_task = asyncio.create_task(
            ui_loop(state, config_provider, on_menu_open, on_qr_open, on_export),
            name="ui",
        )
        worker_tasks = [
            asyncio.create_task(
                hourly_loop(page, state, config_provider, on_config_save_async),
                name="hourly",
            ),
            asyncio.create_task(
                daily_loop(page, state, config_provider, on_config_save_async),
                name="daily",
            ),
            asyncio.create_task(
                gambling_loop(page, state, config_provider, on_config_save_async, db),
                name="gambling",
            ),
            asyncio.create_task(
                nekomusume_loop(page, state, config_provider, on_config_save_async),
                name="neko",
            ),
            asyncio.create_task(
                transfer_loop(page, state, config_provider, on_config_save_async),
                name="transfer",
            ),
            asyncio.create_task(
                digest_loop(state, config_provider, on_config_save_async),
                name="digest",
            ),
        ]

        await ui_task

        # ── Graceful shutdown ────────────────────────────────────
        # 先設 quit 旗標讓 loop 自行退出(避免 cancel 中斷半完成操作)
        async with state.lock:
            state.quit = True
        await asyncio.sleep(0.5)
        # 尚未自動退出的就 cancel
        for t in worker_tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)

        # 13) 關 dashboard
        if dashboard_thread is not None:
            try:
                from bot.web.dashboard import stop_dashboard_thread
                stop_dashboard_thread(dashboard_thread)
            except Exception:    # noqa: BLE001
                log.warning("關閉 dashboard 失敗", exc_info=True)

        await browser.close()

        # 14) 最後保存
        try:
            await db.save_slot_analysis(state.slot_analysis)
        except Exception:    # noqa: BLE001
            log.warning("最終儲存 slot_analysis 失敗", exc_info=True)
        log.info("程式已結束(slot 分析 / 歷史紀錄已儲存到 DB)")

        if state.reboot:
            log.info("Reboot 已請求,以 exit code %d 退出", REBOOT_EXIT_CODE)
            sys.exit(REBOOT_EXIT_CODE)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

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
from bot.core.config import (
    BotConfig,
    load_config,
    merge_partial,
    migrate_analysis_from_json,
    migrate_from_json_if_needed,
    migrate_history_from_json,
    save_config,
)
from bot.core.constants import (
    DATA_DIR,
    DB_PATH,
    LEGACY_ANALYSIS_PATH,
    LEGACY_CONFIG_PATH,
    LEGACY_HISTORY_PATH,
    LEGACY_LOG_FILE_PATH,
    LEGACY_SLOT_DEBUG_LOG_PATH,
    LEGACY_STORAGE_STATE_PATH,
    LOG_DIR,
    LOG_FILE_BACKUP_COUNT,
    LOG_FILE_MAX_BYTES,
    LOG_FILE_PATH,
    REBOOT_EXIT_CODE,
    REBOOT_FLAG_PATH,
    SLOT_DEBUG_LOG_PATH,
    STORAGE_STATE_PATH,
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
from bot.scheduler.news import news_loop
from bot.scheduler.stock import stock_loop
from bot.scheduler.transfer import transfer_loop
from bot.scheduler.updater import updater_loop
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
            # WARNING+ 也 push 到 error_lines 給除錯專區看
            if record.levelno >= logging.WARNING:
                from datetime import datetime as _dt
                self.state.error_lines.append({
                    "ts":     _dt.now().strftime("%H:%M:%S"),
                    "level":  record.levelname,
                    "logger": record.name,
                    "msg":    record.getMessage()[:300],
                })
        except (TypeError, ValueError):
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
        # 若 LOG_DIR 還不存在(例如外部呼叫 setup_logging 沒走 _bootstrap_layout)
        # 也保險建一下,避免 RotatingFileHandler 開檔失敗
        os.makedirs(LOG_DIR, exist_ok=True)
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


def _bootstrap_layout() -> None:
    """建立 data/ + logs/ 資料夾,並把舊路徑的檔案搬到新位置。

    為什麼要搬?方便備份 / 排除 / 清理 — runtime 資料全在 data/、所有 log
    在 logs/、原始碼留在根目錄。

    舊位置 → 新位置:
      storage_state.json  → data/storage_state.json
      bot.log + .1/.2/.3  → logs/bot.log + .1/.2/.3
      slot_debug.log      → logs/slot_debug.log

    搬移用 rename 而不是 copy+delete,避免「中斷時兩邊都有」。新位置已存在
    就跳過(代表先前已搬過或新建)。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR,  exist_ok=True)

    moves = [
        (LEGACY_STORAGE_STATE_PATH,  STORAGE_STATE_PATH),
        (LEGACY_LOG_FILE_PATH,       LOG_FILE_PATH),
        (LEGACY_SLOT_DEBUG_LOG_PATH, SLOT_DEBUG_LOG_PATH),
    ]
    # bot.log 的輪替檔(.1 .2 .3)也一起搬
    for i in range(1, LOG_FILE_BACKUP_COUNT + 1):
        moves.append((f"{LEGACY_LOG_FILE_PATH}.{i}", f"{LOG_FILE_PATH}.{i}"))

    for src, dst in moves:
        if not os.path.exists(src):
            continue
        if os.path.exists(dst):
            # 兩邊都有 → 新位置(dst)是 canonical;src 是 legacy 殘留,
            # 通常代表先前已搬過、或上次搬移過程中斷。直接刪掉 src,
            # 不要保留兩份避免使用者誤以為 root bot.log 還在用。
            # 對 storage_state.json 來說一樣安全 — dst 是當前 session 路徑。
            try:
                os.remove(src)
                print(f"  ✓ 清掉殘留 {src}(已有新位置 {dst})")
            except OSError as e:
                print(f"  ⚠ 無法清除 legacy {src}: {e}")
            continue
        try:
            os.replace(src, dst)
            print(f"  ✓ 已搬移 {src} → {dst}")
        except OSError as e:
            print(f"  ⚠ 搬移失敗 {src} → {dst}: {e}")


# ── 主程式 ────────────────────────────────────────────────────────────
async def main() -> int:
    """執行 bot 主流程。回傳 exit code(0=正常結束,42=請求重啟)。

    重要:reboot 時 sys.exit 必須在 async_playwright context 之外呼叫,
    否則 SystemExit 會穿過 async 清理路徑、可能被吞掉或讓子程序殘留,
    結果是 run.bat 看不到 errorlevel 42、不會自動 loop 重啟(F 鍵閃退)。
    """
    # 0) 建立資料夾結構 + 搬舊檔案到新位置
    _bootstrap_layout()

    # 0.5) 清掉上次留下的 reboot sentinel(run.bat 應該已經 del,但保險再做一次)
    try:
        if os.path.exists(REBOOT_FLAG_PATH):
            os.remove(REBOOT_FLAG_PATH)
    except OSError:
        pass

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

    # 一次性 migration:把 stock_news.news_date 從 YYYY/M/D 變 ISO YYYY-MM-DD,
    # 跨 sym 字串排序才正確。跑過一次後 meta 記錄不重複跑。
    try:
        n_news_iso = await db.migrate_news_dates_to_iso_if_needed()
        if n_news_iso:
            print(f"  ✓ 已 normalize {n_news_iso} 則新聞日期到 ISO 格式")
    except Exception:    # noqa: BLE001
        print("  ⚠ news date migration 失敗(可忽略,新項仍會用新格式)")

    # 一次性 migration:清掉 stock_prices 中 parser 污染 row(明顯離譜值)
    try:
        n_outliers = await db.cleanup_stock_prices_outliers_if_needed()
        if n_outliers:
            print(f"  ✓ 已清除 {n_outliers} 筆 stock_prices 異常價格"
                  f"(parser 污染 row,sym 平均 100x 以上 ratio)")
    except Exception:    # noqa: BLE001
        print("  ⚠ stock_prices outlier cleanup 失敗(可忽略)")

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

        # navigate 失敗計數器(跨 reboot 持久化)— 防止 wizard 無限循環。
        # session 過期 / 找不到輸入框 / 其他暫時性網路問題 都會觸發 reboot,
        # 連續 3 次仍失敗就停下來,讓使用者手動排查。
        _NAV_FAIL_KEY = "consecutive_nav_failures"
        try:
            nav_fails_str = await db.get_meta(_NAV_FAIL_KEY) or "0"
            nav_fails = int(nav_fails_str) if nav_fails_str.isdigit() else 0
        except (TypeError, ValueError):
            nav_fails = 0

        try:
            await navigate_to_channel(page, config.guild_id, config.channel_id)
            # 成功 → reset counter
            if nav_fails > 0:
                await db.set_meta(_NAV_FAIL_KEY, "0")
        except RuntimeError as e:
            await browser.close()
            nav_fails += 1
            await db.set_meta(_NAV_FAIL_KEY, str(nav_fails))

            # 連續失敗達上限 → 停下來請使用者手動處理,避免 wizard 無限循環
            if nav_fails >= 3:
                print()
                print("=" * 70)
                print(f"  ⚠ 連續 {nav_fails} 次 navigate 失敗 — 已停止自動重啟")
                print("=" * 70)
                print("  可能原因(不是 session 問題):")
                print("    • channel_id / guild_id 設定錯誤(進主程式按 C 修正)")
                print("    • Discord 改 UI(輸入框 selector 變了 — 找作者修)")
                print("    • 網路長時間不穩(等等再試)")
                print(f"\n  錯誤詳情: {e}")
                print()
                print("  排查完後手動跑 run.bat 重試;")
                print("  若想重新登入,先刪 data\\storage_state.json")
                # exit code 1,run.bat 不會自動 reboot
                return 1

            # 否則:刪 storage_state + 跑登入精靈 + reboot
            # session 過期是最常見原因,但即使原因不同(暫時的網路、Discord
            # 後端抖動)讓使用者重登一次也是合理的恢復路徑
            log.warning("navigate 第 %d 次失敗(%s),刪 storage_state 跑登入精靈",
                        nav_fails, e)
            if os.path.exists(STORAGE_STATE_PATH):
                await remove_file(STORAGE_STATE_PATH)
            await _run_login_wizard()
            print("\n  ✓ 重新登入完成,3 秒後自動重啟 bot...")
            await asyncio.sleep(3)
            # 設 reboot 旗標,讓外層回傳 exit code(避免在 async with 內 sys.exit)
            state.reboot = True
            return REBOOT_EXIT_CODE

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
                if action == "stock_refresh":
                    state.stock_force_poll = True
                    state.queue_log("📡 Dashboard:請求立即重 poll 股票")
                    return {"ok": True, "message": "已請求立即重 poll(30 秒內生效)"}
                return {"ok": False, "message": f"未知動作: {action}"}
            except Exception as e:    # noqa: BLE001
                log.exception("on_action 失敗")
                return {"ok": False, "message": f"錯誤: {e}"}

        def on_config_save_sync(partial: dict) -> dict:
            """Dashboard thread → main loop 的 sync 包裝。

            交易式更新:
              1. deepcopy 目前 config(避免 in-place mutation)
              2. merge_partial 套到 copy 上、跑驗證
              3. 驗證 OK 才 schedule save_config 到 main loop
              4. DB 寫入成功後才把 copy 置換到 config_holder[0]
            驗證或 DB 寫入失敗都不會碰到記憶體中的真實 config。
            """
            import copy as _copy
            try:
                cur = config_holder[0]
                trial = _copy.deepcopy(cur)        # ← 改動 trial,不碰 cur
                errs = merge_partial(trial, partial)
                if errs:
                    return {"ok": False, "errors": errs, "message": "驗證失敗"}
                # 寫 DB 用 copy(若失敗 cur 仍乾淨)
                fut = asyncio.run_coroutine_threadsafe(save_config(db, trial), loop)
                warnings = fut.result(timeout=5) or []
                # 只有 DB 寫入成功才置換 config_holder
                config_holder[0] = trial
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
                    main_loop=loop,    # ← 傳入 main loop 以便 thread-safe snapshot
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
                daily_loop(page, state, config_provider, on_config_save_async, db),
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
            asyncio.create_task(
                updater_loop(state, config_provider, on_config_save_async),
                name="updater",
            ),
            asyncio.create_task(
                stock_loop(page, state, config_provider, on_config_save_async, db),
                name="stock",
            ),
            asyncio.create_task(
                news_loop(page, state, config_provider, on_config_save_async, db),
                name="news",
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
        reboot_requested = state.reboot

    # ── 已退出 async_playwright context;清理已完成 ──────────────────
    if reboot_requested:
        # 1. 寫 sentinel 檔案 — run.bat 用這個判斷是否要重啟(exit code 在
        #    Rich Live alt-screen 切換後不一定可靠,sentinel 是雙保險)
        try:
            os.makedirs(os.path.dirname(REBOOT_FLAG_PATH), exist_ok=True)
            with open(REBOOT_FLAG_PATH, "w", encoding="utf-8") as f:
                f.write("1")
        except OSError as exc:
            log.warning("寫入 reboot sentinel 失敗: %s", exc)

        log.info("Reboot 已請求,以 exit code %d 退出", REBOOT_EXIT_CODE)

        # 2. flush 所有 logging handler — 確保 bot.log 寫進最終訊息
        for h in logging.getLogger().handlers:
            try:
                h.flush()
            except Exception:    # noqa: BLE001
                pass
        sys.stdout.flush()
        sys.stderr.flush()

        # 3. 強制離開 alternate screen 模式(Rich Live screen=True 退出
        #    時應該已經做過,但偶爾會殘留 — 多發一次無害,讓 run.bat 的
        #    echo 能正常顯示在主 console buffer)
        if os.name == "nt":
            try:
                sys.stdout.write("\x1b[?1049l\x1b[0m")
                sys.stdout.flush()
            except OSError:
                pass

        # 4. Banner 讓使用者看到 Python 真的有跑到 reboot 路徑
        print("\n" + "=" * 50)
        print(f"  REBOOT REQUESTED -- exit code {REBOOT_EXIT_CODE}")
        print("=" * 50, flush=True)
        return REBOOT_EXIT_CODE
    return 0


if __name__ == "__main__":
    rc = 0
    try:
        result = asyncio.run(main())
        rc = result if isinstance(result, int) else 0
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc)

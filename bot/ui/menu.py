"""互動式設定選單 — 全部使用 input_validation 防呆。

設計:
- 所有輸入走 ask_int / ask_float / ask_choice / ask_user_id 等,自動拒絕中文 / 全形 / 範圍外
- 每次「儲存並返回」會跑 schema.validate(),不通過就顯示錯誤並讓使用者修正
- 敏感欄位(密碼)不會顯示明文,只顯示「已設定 / 未設定」
- 子選單分:賭博 / 目標停損 / Email / 貓娘 / 轉帳 / Dashboard / 進階
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Awaitable, Callable

from bot.core.constants import (
    DEFAULT_BIGWIN_MULTIPLIER,
    DEFAULT_DEAD_THRESHOLD,
    DEFAULT_INTERVAL_MAX,
    DEFAULT_INTERVAL_MIN,
    DEFAULT_NEKO_INTERVAL_MIN,
    DEFAULT_TRANSFER_INTERVAL_MIN,
    EXPORT_DIR,
    LOG_FILE_BACKUP_COUNT,
    LOG_FILE_PATH,
    SLOT_DEBUG_LOG_PATH,
)
from bot.core.state import BotState
from bot.slot.analysis import make_slot_analysis
from bot.ui.input_validation import (
    ainput,
    ask_choice,
    ask_float,
    ask_host,
    ask_int,
    ask_text,
    ask_user_id,
    ask_yes_no,
    wait_enter,
)

if TYPE_CHECKING:
    from bot.core.config import BotConfig
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


# ── 子選單:賭博 ─────────────────────────────────────────────────────
async def _sub_menu_gambling(config: "BotConfig") -> None:
    g = config.gambling
    while True:
        os.system("cls")
        print(f"\n{'═'*48}\n  🎰 賭博基本設定\n{'═'*48}")
        print(f"   [1] 賭博啟用:      {'✓ 啟用' if g.enabled else '✗ 停用'}")
        print(f"   [2] 策略:          {g.strategy}  (auto/fixed/kelly)")
        print(f"   [3] 保底門檻:      {g.threshold:,}")
        print(f"   [4] 最小下注:      {g.min_bet:,}")
        print(f"   [5] 最大下注:      {g.max_bet:,}  (0=自動)")
        print(f"   [6] auto 押注比例: {g.bet_fraction*100:.1f}%")
        print(f"   [7] 下注間距:      {g.interval_min}-{g.interval_max} 秒")
        print()
        print("   [0] 返回主選單")
        choice = (await ainput("\n  選擇: ")).strip()
        if choice == "0":
            return
        elif choice == "1":
            g.enabled = not g.enabled
            print(f"  ✓ 賭博 → {'啟用' if g.enabled else '停用'}")
            await wait_enter()
        elif choice == "2":
            v = await ask_choice("策略", ["auto", "fixed", "kelly"], g.strategy)
            if v: g.strategy = v
            await wait_enter()
        elif choice == "3":
            v = await ask_int("保底門檻", g.threshold, min_val=0, allow_negative=False)
            if v is not None: g.threshold = v
            await wait_enter()
        elif choice == "4":
            v = await ask_int("最小下注", g.min_bet, min_val=1, allow_negative=False)
            if v is not None: g.min_bet = v
            await wait_enter()
        elif choice == "5":
            v = await ask_int("最大下注(0=自動)", g.max_bet,
                              min_val=0, allow_negative=False)
            if v is not None: g.max_bet = v
            await wait_enter()
        elif choice == "6":
            v = await ask_float("押注比例(%,例 2 = 2%)", g.bet_fraction * 100,
                                min_val=0, max_val=100)
            if v is not None: g.bet_fraction = v / 100
            await wait_enter()
        elif choice == "7":
            print(f"  目前: {g.interval_min}-{g.interval_max} 秒")
            mn = await ask_float("最小間距秒數", g.interval_min, min_val=0)
            mx = await ask_float("最大間距秒數", g.interval_max, min_val=0)
            if mn is not None: g.interval_min = mn
            if mx is not None: g.interval_max = mx
            if g.interval_max < g.interval_min:
                g.interval_max = g.interval_min
            await wait_enter()


# ── 子選單:目標 / 停損 / 連敗冷靜 ──────────────────────────────────
async def _sub_menu_goals(config: "BotConfig", state: BotState) -> None:
    g = config.gambling
    while True:
        os.system("cls")
        print(f"\n{'═'*48}\n  🏁 目標 / 停損 / 連敗冷靜\n{'═'*48}")
        print("  [停利]")
        print(f"   [1] 目標餘額:        {g.goal:,}  (0=不設目標)")
        print(f"   [2] 達標行為:        {g.goal_action}  (pause/raise)")
        print(f"   [3] raise 步進:      {g.goal_step:,}")
        print()
        print("  [停損]")
        print(f"   [4] 停損點:          {g.loss_floor:,}  (0=不設停損)")
        print(f"   [5] 停損行為:        {g.loss_action}")
        print(f"   [6] 階梯下移步進:    {g.loss_step:,}")
        print()
        print("  [連敗冷靜]")
        sp = g.loss_streak_pause
        print(f"   [7] 連敗 N 場觸發:   {sp if sp else '停用'}  (0=停用)")
        print(f"   [8] 冷靜分鐘:        {g.loss_streak_cooldown_min}")
        print()
        print(f"   [9] 通知 UID:        {g.notify_user_id or '(未設定)'}")
        print()
        print("   [0] 返回主選單")
        choice = (await ainput("\n  選擇: ")).strip()
        if choice == "0":
            return
        elif choice == "1":
            v = await ask_int("目標餘額(0=取消)", g.goal,
                              min_val=0, allow_negative=False)
            if v is not None:
                g.goal = v
                async with state.lock:
                    state.goal_reached = False
            await wait_enter()
        elif choice == "2":
            v = await ask_choice("達標行為", ["pause", "raise"], g.goal_action)
            if v: g.goal_action = v
            await wait_enter()
        elif choice == "3":
            v = await ask_int("raise 步進", g.goal_step,
                              min_val=0, allow_negative=False)
            if v is not None: g.goal_step = v
            await wait_enter()
        elif choice == "4":
            v = await ask_int("停損點(0=取消)", g.loss_floor,
                              min_val=0, allow_negative=False)
            if v is not None:
                g.loss_floor = v
                async with state.lock:
                    state.loss_triggered = False
            await wait_enter()
        elif choice == "5":
            v = await ask_choice("停損行為", ["pause", "lower_threshold"], g.loss_action)
            if v: g.loss_action = v
            await wait_enter()
        elif choice == "6":
            v = await ask_int("階梯下移步進", g.loss_step,
                              min_val=0, allow_negative=False)
            if v is not None: g.loss_step = v
            await wait_enter()
        elif choice == "7":
            v = await ask_int("連敗 N 場後暫停(0=停用)", sp,
                              min_val=0, allow_negative=False)
            if v is not None: g.loss_streak_pause = v
            await wait_enter()
        elif choice == "8":
            v = await ask_float("冷靜分鐘", g.loss_streak_cooldown_min, min_val=0)
            if v is not None: g.loss_streak_cooldown_min = v
            await wait_enter()
        elif choice == "9":
            v = await ask_user_id("Discord User ID", g.notify_user_id)
            if v is not None: g.notify_user_id = v
            await wait_enter()


# ── 子選單:Email ────────────────────────────────────────────────────
async def _sub_menu_email(config: "BotConfig") -> None:
    e = config.email
    g = config.gambling
    while True:
        os.system("cls")
        bw_mul = float(g.bigwin_multiplier or DEFAULT_BIGWIN_MULTIPLIER)
        pwd_set = bool((e.password or "").strip())
        print(f"\n{'═'*48}\n  📧 Email / 通知\n{'═'*48}")
        print(f"   [1] Email 主開關:    {'✓ 啟用' if e.enabled else '✗ 停用'}")
        print(f"   [2] 收件人:          {e.to or '(未設定)'}")
        print(f"   [3] SMTP 設定        host={e.smtp_host} port={e.smtp_port}")
        print(f"   [4] 寄件人帳密       user={e.user or '(未設定)'}, "
              f"密碼={'已設定' if pwd_set else '(未設)'}")
        print()
        print("  [通知種類]")
        print(f"   [5] 達標通知:        {'✓' if e.notify_goal else '✗'}")
        print(f"   [6] 停損通知:        {'✓' if e.notify_loss else '✗'}")
        print(f"   [7] 中大獎通知:      {'✓' if e.notify_bigwin else '✗'}  賠率門檻={bw_mul:.1f}x")
        print(f"   [8] 停擺通知:        {'✓' if e.notify_dead else '✗'}  失敗門檻={e.dead_threshold}")
        print(f"   [9] 貓娘完成通知:    {'✓' if e.notify_neko else '✗'}")
        print(f"   [A] 每日摘要:        {'✓' if e.notify_digest else '✗'}  時段={e.digest_hour:02d}:00")
        print()
        print("   [0] 返回主選單")
        choice = (await ainput("\n  選擇: ")).strip().upper()
        if choice == "0":
            return
        elif choice == "1":
            e.enabled = not e.enabled
            print(f"  ✓ Email → {'啟用' if e.enabled else '停用'}")
            await wait_enter()
        elif choice == "2":
            v = await ask_text("收件人", e.to,
                                max_len=200, allow_chinese=False, allow_empty=False)
            if v is not None: e.to = v
            await wait_enter()
        elif choice == "3":
            host = await ask_text("SMTP host", e.smtp_host,
                                  max_len=200, allow_chinese=False, allow_empty=False)
            port = await ask_int("SMTP port", e.smtp_port, min_val=1, max_val=65535)
            if host: e.smtp_host = host
            if port is not None: e.smtp_port = port
            await wait_enter()
        elif choice == "4":
            user = await ask_text("寄件人 user", e.user,
                                  max_len=200, allow_chinese=False, allow_empty=False)
            print("  寄件人 password (Gmail 用 App Password;Enter 跳過):")
            pwd  = (await ainput("  密碼: ")).rstrip("\r\n")
            if user is not None: e.user = user
            if pwd: e.password = pwd
            print("  ✓ 帳密已更新")
            await wait_enter()
        elif choice == "5":
            e.notify_goal = not e.notify_goal
            await wait_enter()
        elif choice == "6":
            e.notify_loss = not e.notify_loss
            await wait_enter()
        elif choice == "7":
            e.notify_bigwin = not e.notify_bigwin
            v = await ask_float("賠率門檻 (>=幾倍才寄信)", bw_mul, min_val=1.0)
            if v is not None: g.bigwin_multiplier = v
            await wait_enter()
        elif choice == "8":
            e.notify_dead = not e.notify_dead
            v = await ask_int("連續失敗幾次算停擺", e.dead_threshold, min_val=1)
            if v is not None: e.dead_threshold = v
            await wait_enter()
        elif choice == "9":
            e.notify_neko = not e.notify_neko
            await wait_enter()
        elif choice == "A":
            e.notify_digest = not e.notify_digest
            v = await ask_int("摘要時段(0~23 整點)", e.digest_hour, min_val=0, max_val=23)
            if v is not None: e.digest_hour = v
            await wait_enter()


# ── 子選單:貓娘 ────────────────────────────────────────────────────
async def _sub_menu_neko(config: "BotConfig") -> None:
    n = config.nekomusume
    while True:
        os.system("cls")
        print(f"\n{'═'*48}\n  🐱 貓娘監控\n{'═'*48}")
        print(f"   [1] 監控啟用:        {'✓' if n.enabled else '✗'}")
        print(f"   [2] 檢查間距:        {n.check_interval_min} 分鐘")
        print(f"   [3] 自動領取再派遣:  {'✓' if n.auto_claim else '✗'}")
        print()
        print("   [0] 返回主選單")
        choice = (await ainput("\n  選擇: ")).strip()
        if choice == "0":
            return
        elif choice == "1":
            n.enabled = not n.enabled
            await wait_enter()
        elif choice == "2":
            v = await ask_float("檢查間距分鐘 (建議 15-60)",
                                 n.check_interval_min, min_val=1)
            if v is not None: n.check_interval_min = v
            await wait_enter()
        elif choice == "3":
            n.auto_claim = not n.auto_claim
            if n.auto_claim:
                print("  ⚠ 自動領取會送 /nekomusume status 並點「領取並再派遣」按鈕")
            await wait_enter()


# ── 子選單:轉帳 ────────────────────────────────────────────────────
async def _sub_menu_transfer(config: "BotConfig") -> None:
    t = config.transfer
    while True:
        os.system("cls")
        print(f"\n{'═'*48}\n  💸 自動轉帳\n{'═'*48}")
        print(f"   [1] 啟用:            {'✓' if t.enabled else '✗'}")
        print(f"   [2] 對象:            {t.target or '(未設定)'}")
        print(f"   [3] 金額:            {t.amount:,}")
        print(f"   [4] 間距分鐘:        {t.interval_min}")
        print()
        print("   [0] 返回主選單")
        choice = (await ainput("\n  選擇: ")).strip()
        if choice == "0":
            return
        elif choice == "1":
            t.enabled = not t.enabled
            await wait_enter()
        elif choice == "2":
            print("  注意:對象是 Discord user picker 搜尋字串(顯示名稱片段或 user ID)")
            v = await ask_text("對象", t.target,
                                max_len=200, allow_chinese=True, allow_empty=False)
            if v is not None: t.target = v
            await wait_enter()
        elif choice == "3":
            v = await ask_int("金額", t.amount, min_val=1, allow_negative=False)
            if v is not None: t.amount = v
            await wait_enter()
        elif choice == "4":
            v = await ask_float("間距分鐘", t.interval_min, min_val=1)
            if v is not None: t.interval_min = v
            await wait_enter()


# ── 子選單:Dashboard ───────────────────────────────────────────────
async def _sub_menu_dashboard(config: "BotConfig") -> None:
    d = config.dashboard
    while True:
        os.system("cls")
        pwd_set = bool((d.password or "").strip())
        # 安全警示:0.0.0.0 + 無密碼 = 危險
        warning = ""
        if d.host == "0.0.0.0" and not pwd_set:
            warning = "  ⚠ 警告!0.0.0.0 + 無密碼 = 同 LAN 任何人都能存取!\n"
        print(f"\n{'═'*48}\n  🌐 Web Dashboard\n{'═'*48}")
        if warning:
            print(warning)
        print(f"   [1] 啟用:            {'✓' if d.enabled else '✗'}")
        print(f"   [2] 監聽位址:        {d.host}")
        print("                        (0.0.0.0=同 LAN 都能看;127.0.0.1=只本機)")
        print(f"   [3] Port:            {d.port}")
        print(f"   [4] 帳號:            {d.username}")
        print(f"   [5] 密碼:            {'已設定' if pwd_set else '(未設)'}")
        print()
        print("   [0] 返回主選單")
        choice = (await ainput("\n  選擇: ")).strip()
        if choice == "0":
            # 安全檢查:離開時若 0.0.0.0 但未設密碼,自動退到 127.0.0.1
            if d.enabled and d.host == "0.0.0.0" and not pwd_set:
                print("\n  ⚠ 偵測到 0.0.0.0 但未設密碼 — 自動改為 127.0.0.1 保護")
                d.host = "127.0.0.1"
                await wait_enter()
            return
        elif choice == "1":
            d.enabled = not d.enabled
            await wait_enter()
        elif choice == "2":
            v = await ask_host("監聽位址 (0.0.0.0 / 127.0.0.1 / IPv4)", d.host)
            if v is not None:
                d.host = v
                if v == "0.0.0.0" and not pwd_set:
                    print("  ⚠ 0.0.0.0 + 無密碼非常危險,請務必設定密碼!")
            await wait_enter()
        elif choice == "3":
            v = await ask_int("Port", d.port, min_val=1, max_val=65535)
            if v is not None: d.port = v
            await wait_enter()
        elif choice == "4":
            v = await ask_text("帳號(預設 admin)", d.username,
                                max_len=64, allow_chinese=False, allow_empty=False)
            if v: d.username = v
            await wait_enter()
        elif choice == "5":
            print("  輸入空白 → 移除密碼(若 host=0.0.0.0 強烈不建議)")
            print("  建議使用至少 8 字元的混合字元密碼")
            raw = (await ainput("  新密碼: ")).rstrip("\r\n")
            d.password = raw
            print(f"  ✓ {'已設定' if raw else '已移除'}")
            await wait_enter()


# ── 進階:檔案管理 + 系統更新 ────────────────────────────────────
async def _run_advanced_menu(state: BotState, db: "Database") -> None:
    while True:
        os.system("cls")
        print(f"\n{'═'*48}\n  🛠️  進階設定\n{'═'*48}")

        debug_size = _file_size_str(SLOT_DEBUG_LOG_PATH)
        bot_log_size = _file_size_str(LOG_FILE_PATH)
        exports_size, exports_count = _dir_size_str(EXPORT_DIR)
        sa_spins = (state.slot_analysis or {}).get("total_spins", 0)
        history_count = len(state.history or [])

        print("  [檔案管理]")
        print(f"   [1] 刪除 slot_debug.log              ({debug_size})")
        print(f"   [2] 刪除 exports/ 內所有檔案         ({exports_count} 檔, {exports_size})")
        print(f"   [3] 清空下注歷史(DB)                ({history_count} 筆)")
        print(f"   [4] 重置 slot 分析(DB)              ({sa_spins} 筆)")
        print(f"   [5] 一鍵清除以上全部")
        print()
        print("  [日誌]")
        print(f"   [7] 開啟 bot.log                     ({bot_log_size})")
        print(f"   [8] 清空 bot.log + 輪替檔")
        print()
        print("  [系統]")
        print(f"   [6] 系統更新(git pull + 重啟)")
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
            if await ask_yes_no("⚠ 確認刪除以上所有檔案+重置分析?"):
                from bot.core.async_io import remove_file
                await remove_file(SLOT_DEBUG_LOG_PATH)
                d_count, _ = _delete_dir_contents_sync(EXPORT_DIR)
                await db.clear_history()
                await db.reset_slot_analysis()
                async with state.lock:
                    state.history = []
                    state.slot_analysis = make_slot_analysis()
                print(f"  ✓ 已清除全部(exports {d_count} 檔)")
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


# ── 主選單 ────────────────────────────────────────────────────────────
async def run_config_menu(
    config: "BotConfig",
    state: BotState,
    db: "Database",
    on_save: Callable[["BotConfig"], Awaitable[None]],
) -> None:
    """主設定選單。儲存時會呼叫 on_save(config) 把改動寫到 DB。"""
    while True:
        os.system("cls")
        sa_spins = (state.slot_analysis or {}).get("total_spins", 0)
        print(f"\n{'═'*52}")
        print("  ⚙️  Discord Bot — 系統設定")
        print(f"{'═'*52}")

        print(f"   [1] 🎰 賭博基本     ({config.gambling.strategy}, "
              f"{'啟用' if config.gambling.enabled else '停用'})")
        print(f"   [2] 🏁 目標 / 停損 / 連敗冷靜")
        print(f"   [3] 📧 Email / 通知 ({'啟用' if config.email.enabled else '停用'})")
        print(f"   [4] 🐱 貓娘監控     ({'啟用' if config.nekomusume.enabled else '停用'}, "
              f"自動領取={'✓' if config.nekomusume.auto_claim else '✗'})")
        print(f"   [5] 💸 自動轉帳     ({'啟用' if config.transfer.enabled else '停用'})")
        print(f"   [6] 🌐 Dashboard    ({'啟用' if config.dashboard.enabled else '停用'}, "
              f"密碼={'已設' if config.dashboard.password else '未設'})")
        print(f"   [7] 🛠️  進階(檔案管理 / 系統更新)")
        print()
        print(f"   📊 Slot 分析:       {sa_spins:,} 筆紀錄")
        print()
        print("   [0] 儲存並返回")
        print()

        choice = (await ainput("  選擇: ")).strip()
        if choice == "0":
            break
        elif choice == "1":
            await _sub_menu_gambling(config)
        elif choice == "2":
            await _sub_menu_goals(config, state)
        elif choice == "3":
            await _sub_menu_email(config)
        elif choice == "4":
            await _sub_menu_neko(config)
        elif choice == "5":
            await _sub_menu_transfer(config)
        elif choice == "6":
            await _sub_menu_dashboard(config)
        elif choice == "7":
            await _run_advanced_menu(state, db)
            if state.quit:
                break

    # 驗證 + 儲存
    errs = config.validate()
    if errs:
        print("\n  ⚠ 設定有 %d 個問題:" % len(errs))
        for e in errs:
            print(f"    - {e}")
        print("\n  仍會儲存,但部分功能可能無法正常運作。建議按 C 進入再修正。")
        await wait_enter()
    await on_save(config)
    state.queue_log("設定已更新並儲存")
    os.system("cls")


# ── 首次設定精靈 ─────────────────────────────────────────────────────
async def first_run_wizard(config: "BotConfig") -> bool:
    """首次啟動 / 必填欄位缺失時引導使用者填入。回傳是否完成。

    必填欄位:guild_id / channel_id / notify_user_id。
    Dashboard 密碼若空字串會強制要求(可選擇空+127.0.0.1)。
    """
    needed = []
    if not config.guild_id:
        needed.append("guild_id")
    if not config.channel_id:
        needed.append("channel_id")
    if not config.gambling.notify_user_id:
        needed.append("notify_user_id")

    pwd_missing = (config.dashboard.enabled
                   and not (config.dashboard.password or "").strip())

    if not needed and not pwd_missing:
        return True

    print()
    print("=" * 64)
    print("  🛠️  首次設定 — 請填入下列資訊(按 Enter 跳過保留現值)")
    print("=" * 64)
    print()
    print("  📌 開啟 Discord 開發者模式:使用者設定 → 進階 → 啟用「開發者模式」")
    print("      之後右鍵伺服器/頻道/使用者就會多出「複製 ID」選項")
    print()

    if "guild_id" in needed:
        print(f"  【伺服器 ID】(目前: {config.guild_id or '未設定'})")
        print("    → 對伺服器右鍵 → 複製伺服器 ID")
        v = await ask_user_id("    伺服器 ID", config.guild_id)
        if v: config.guild_id = v

    if "channel_id" in needed:
        print(f"\n  【頻道 ID】(目前: {config.channel_id or '未設定'}) — bot 會在此頻道送指令")
        print("    → 對要操作的頻道右鍵 → 複製頻道 ID")
        v = await ask_user_id("    頻道 ID", config.channel_id)
        if v: config.channel_id = v

    if "notify_user_id" in needed:
        print(f"\n  【通知對象 User ID】(目前: {config.gambling.notify_user_id or '未設定'})")
        print("    → 達成目標 / 貓娘完成時要 @ 的對象(通常填自己)")
        print("    → 對使用者右鍵 → 複製使用者 ID")
        v = await ask_user_id("    User ID", config.gambling.notify_user_id)
        if v: config.gambling.notify_user_id = v

    if pwd_missing:
        print()
        print("=" * 64)
        print("  🔒 Dashboard 安全設定")
        print("=" * 64)
        print()
        print("  Dashboard 目前是「啟用」狀態。為了安全,必須設定密碼。")
        print("  若不設密碼,監聽位址將自動改為 127.0.0.1(只本機可存取)。")
        print()

        if config.dashboard.host == "0.0.0.0":
            print("  目前 host=0.0.0.0(同 LAN 可存取)— 強烈建議設密碼")
        print()
        # 強制流程:要嘛設密碼,要嘛退到 127.0.0.1
        for _ in range(3):
            raw = (await ainput("  新密碼(空白=不設,host 自動退到 127.0.0.1): ")).rstrip("\r\n")
            if raw:
                if len(raw) < 4:
                    print("  ⚠ 密碼太短(至少 4 字),請重設")
                    continue
                config.dashboard.password = raw
                print("  ✓ 密碼已設定")
                break
            else:
                if config.dashboard.host == "0.0.0.0":
                    config.dashboard.host = "127.0.0.1"
                    print("  ✓ host 已改為 127.0.0.1(只本機可存取)")
                else:
                    print("  ✓ 維持目前 host(無密碼)")
                break

    print("=" * 64)
    return True

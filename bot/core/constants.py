"""集中所有 magic numbers / 預設值。

從 main.py 抽出來,讓修改設定不用再翻 3000 行檔案。所有預設值都應只在
這裡定義,設定 dataclass 引用此處,避免三處不同步。
"""
from __future__ import annotations

# ── 檔案路徑 ──────────────────────────────────────────────────────────
# 持久化資料(SQLite + 加密金鑰 + Discord session)集中放 data/
# 所有 log 檔案集中放 logs/
# 避免污染專案根目錄,並讓備份 / 清理 / 排除更直觀
DATA_DIR              = "data"
LOG_DIR               = "logs"
DB_PATH               = "data/bot.db"
SECRET_KEY_PATH       = "data/secret.key"
STORAGE_STATE_PATH    = "data/storage_state.json"
EXPORT_DIR            = "exports"
LOG_FILE_PATH         = "logs/bot.log"
SLOT_DEBUG_LOG_PATH   = "logs/slot_debug.log"

# ── 舊版檔案(用於一次性 migration 後清掉) ─────────────────────────────
# 這些 path 留著只是給 migrate_*_from_json() 找舊檔。已遷移完就可刪除。
# 同時也支援 storage_state.json 從舊根目錄位置升級。
LEGACY_CONFIG_PATH         = "config.json"
LEGACY_HISTORY_PATH        = "gambling_history.json"
LEGACY_ANALYSIS_PATH       = "slot_analysis.json"
LEGACY_STORAGE_STATE_PATH  = "storage_state.json"
LEGACY_LOG_FILE_PATH       = "bot.log"
LEGACY_SLOT_DEBUG_LOG_PATH = "slot_debug.log"

# ── 日誌 ──────────────────────────────────────────────────────────────
LOG_FILE_MAX_BYTES    = 5 * 1024 * 1024   # 5 MB
LOG_FILE_BACKUP_COUNT = 3                  # bot.log.1 .2 .3
UI_LOG_LINES_MAX      = 25                 # UI 日誌面板最多保留行數

# ── 排程 ──────────────────────────────────────────────────────────────
HOURLY_POST_BOUNDARY_MIN_SEC =  30
HOURLY_POST_BOUNDARY_MAX_SEC = 180
# /daily 改成錨定每日 00:00 觸發(daily.py 內自己算,不用這幾個常數了)
GAMBLE_RECHECK_SEC           = 300
# raise 模式達標時:new_threshold = goal - GOAL_RAISE_THRESHOLD_BUFFER
# 保留 10000 緩衝給 bot 下注,不會立刻撞保底
GOAL_RAISE_THRESHOLD_BUFFER  = 10000

# ── 重啟 ──────────────────────────────────────────────────────────────
REBOOT_EXIT_CODE                          = 42
# Sentinel 檔案 — Python 退出前寫入,run.bat 讀取後決定是否 loop 重啟。
# 跟 exit code 42 一起作為雙保險:exit code 在某些 Windows 終端環境(尤其
# Rich Live alternate-screen 切換)可能取不到正確值,sentinel 檔案是
# rock-solid signal。
REBOOT_FLAG_PATH                          = "data/.reboot"
RECOVER_PAGE_FAILS_BEFORE_BROWSER_RESTART = 3

# ── 打字節奏 ──────────────────────────────────────────────────────────
TYPING_DELAY_MIN_MS = 50
TYPING_DELAY_MAX_MS = 150

# ── 預設設定值 ────────────────────────────────────────────────────────
DEFAULT_NOTIFY_USER_ID        = "429881182168023040"
DEFAULT_INTERVAL_MIN          = 4
DEFAULT_INTERVAL_MAX          = 10
DEFAULT_GOAL                  = 0
DEFAULT_GOAL_ACTION           = "pause"
DEFAULT_GOAL_STEP             = 10000
DEFAULT_LOSS_FLOOR            = 0
DEFAULT_LOSS_ACTION           = "pause"
DEFAULT_LOSS_STEP             = 5000
DEFAULT_NEKO_INTERVAL_MIN     = 30
DEFAULT_BIGWIN_MULTIPLIER     = 5.0
DEFAULT_DEAD_THRESHOLD        = 2
DEFAULT_TRANSFER_INTERVAL_MIN = 60
DEFAULT_DIGEST_HOUR           = 0

# ── Dashboard ─────────────────────────────────────────────────────────
DEFAULT_DASHBOARD_HOST     = "127.0.0.1"   # 安全預設;有設密碼才允許 0.0.0.0
DEFAULT_DASHBOARD_PORT     = 8765
DEFAULT_DASHBOARD_USERNAME = "admin"

# ── Kelly / slot 分析 ─────────────────────────────────────────────────
MIN_KELLY_SAMPLES         = 200            # 提高到 200 才開 Kelly(原 50 過早)
KELLY_MAX_FRACTION        = 0.25           # f* 上限 25%(防爆倉)
KELLY_USE_CONFIDENCE      = True           # True = 用 95% CI 下界估算 Kelly(更保守)
PAYOUT_BUCKETS            = ["0", "0~2", "2~5", "5~8", "8~10", "10~20", "以上"]
HIGH_MULT_THRESHOLD       = 20.0
HIGH_MULT_KEEP            = 50
HISTORY_MAX_LEN           = 5000
SYMBOL_DISPLAY_THRESHOLD  = 0.001

# ── 餘額讀取 ──────────────────────────────────────────────────────────
REPLY_WINDOW_CHARS = 10000

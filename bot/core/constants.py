"""集中所有 magic numbers / 預設值。

從 main.py 抽出來,讓修改設定不用再翻 3000 行檔案。所有預設值都應只在
這裡定義,設定 dataclass 引用此處,避免三處不同步。
"""
from __future__ import annotations

# ── 檔案路徑 ──────────────────────────────────────────────────────────
DB_PATH               = "bot.db"
SECRET_KEY_PATH       = "secret.key"
STORAGE_STATE_PATH    = "storage_state.json"
EXPORT_DIR            = "exports"
LOG_FILE_PATH         = "bot.log"
SLOT_DEBUG_LOG_PATH   = "slot_debug.log"

# ── 舊版檔案(用於一次性 migration 後清掉) ─────────────────────────────
LEGACY_CONFIG_PATH    = "config.json"
LEGACY_HISTORY_PATH   = "gambling_history.json"
LEGACY_ANALYSIS_PATH  = "slot_analysis.json"

# ── 日誌 ──────────────────────────────────────────────────────────────
LOG_FILE_MAX_BYTES    = 5 * 1024 * 1024   # 5 MB
LOG_FILE_BACKUP_COUNT = 3                  # bot.log.1 .2 .3
UI_LOG_LINES_MAX      = 25                 # UI 日誌面板最多保留行數

# ── 排程 ──────────────────────────────────────────────────────────────
HOURLY_POST_BOUNDARY_MIN_SEC =  30
HOURLY_POST_BOUNDARY_MAX_SEC = 180
DAILY_BASE_SEC               = 86400
DAILY_JITTER_SEC             = 45 * 60
DAILY_STARTUP_DELAY_SEC      = 300
GAMBLE_RECHECK_SEC           = 300

# ── 重啟 ──────────────────────────────────────────────────────────────
REBOOT_EXIT_CODE                          = 42
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

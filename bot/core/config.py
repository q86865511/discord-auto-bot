"""設定 schema(dataclasses)+ 驗證 + 持久化(SQLite)。

設計:
- 每個區段一個 dataclass(GamblingConfig、EmailConfig、…)
- 集合在 BotConfig 裡
- 驗證:
    - dataclass `__post_init__` 做型別 / 範圍檢查
    - `validate()` method 回傳錯誤訊息 list,跨欄位檢查放這裡
- 持久化:
    - 從 DB 讀:`load_config(db)`
    - 存到 DB:`save_config(db, config)`(會把敏感欄位加密成 secrets)
- 一次性 migration:`migrate_from_json(db, path)` — 讀舊 config.json 寫到 DB

敏感欄位列表(會存到 secrets table、加密儲存,不放在 config.payload):
    email.password, dashboard.password
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from .constants import (
    DEFAULT_BIGWIN_MULTIPLIER,
    DEFAULT_DASHBOARD_HOST,
    DEFAULT_DASHBOARD_PORT,
    DEFAULT_DASHBOARD_USERNAME,
    DEFAULT_DEAD_THRESHOLD,
    DEFAULT_DIGEST_HOUR,
    DEFAULT_GOAL,
    DEFAULT_GOAL_ACTION,
    DEFAULT_GOAL_STEP,
    DEFAULT_INTERVAL_MAX,
    DEFAULT_INTERVAL_MIN,
    DEFAULT_LOSS_ACTION,
    DEFAULT_LOSS_FLOOR,
    DEFAULT_LOSS_STEP,
    DEFAULT_NEKO_INTERVAL_MIN,
    DEFAULT_TRANSFER_INTERVAL_MIN,
)

log = logging.getLogger(__name__)


# 哪些欄位算敏感資料(會單獨加密存到 secrets 表,不放在 config payload)
SENSITIVE_FIELDS = {
    ("email", "password"),
    ("dashboard", "password"),
}


# ── 各 section dataclass ─────────────────────────────────────────────
@dataclass
class GamblingConfig:
    enabled: bool = True
    threshold: int = 5000
    min_bet: int = 100
    max_bet: int = 500
    strategy: str = "auto"             # auto / fixed / kelly
    bet_fraction: float = 0.02
    interval_min: float = float(DEFAULT_INTERVAL_MIN)
    interval_max: float = float(DEFAULT_INTERVAL_MAX)
    goal: int = DEFAULT_GOAL
    goal_action: str = DEFAULT_GOAL_ACTION  # pause / raise
    goal_step: int = DEFAULT_GOAL_STEP
    loss_floor: int = DEFAULT_LOSS_FLOOR
    loss_action: str = DEFAULT_LOSS_ACTION  # pause / lower_threshold
    loss_step: int = DEFAULT_LOSS_STEP
    loss_streak_pause: int = 0
    loss_streak_cooldown_min: float = 5.0
    bigwin_multiplier: float = DEFAULT_BIGWIN_MULTIPLIER
    notify_user_id: str = ""

    # ── 進階策略(全部 opt-in,預設停用) ──────────────────────────
    # 1) 時段過濾 — 跳過歷史 EV/勝率差的小時
    hourly_filter_enabled: bool = False
    hourly_min_bets:        int   = 50      # 該小時樣本 < N → 不過濾
    hourly_min_winrate:     float = 0.30    # 勝率 < 30% → skip 該小時
    hourly_min_ev:          float = 0.95    # EV < 0.95 → skip 該小時
    # 2) 滾動視窗 EV — 近期 EV 差時減碼、好時加碼
    rolling_enabled:    bool  = False
    rolling_window_size: int  = 500
    rolling_low_ev:     float = 0.95
    rolling_high_ev:    float = 1.02
    rolling_low_mult:   float = 0.5
    rolling_high_mult:  float = 1.5
    # 3) Trailing stop — 從累計淨收峰值跌幅超過 X% → 暫停 N 分鐘
    trailing_stop_enabled:       bool  = False
    trailing_stop_pct:           float = 10.0   # 跌幅門檻 %(預設 10% 比 5% 寬鬆)
    trailing_stop_cooldown_min:  float = 30.0   # 觸發後冷卻幾分鐘
    # 重設策略:冷卻結束時 baseline 跳到當下 history 長度,後續 drawdown
    # 重新從 0 算起。這樣不會因為「peak 還是過去歷史最高」而立刻又觸發。

    def validate(self) -> list[str]:
        errs: list[str] = []
        if self.threshold < 0:
            errs.append("保底門檻不可為負")
        if self.min_bet < 1:
            errs.append("最小下注必須 ≥ 1")
        if self.max_bet < 0:
            errs.append("最大下注不可為負")
        if 0 < self.max_bet < self.min_bet:
            errs.append(f"最大下注({self.max_bet:,})必須 ≥ 最小下注({self.min_bet:,})")
        if not 0 <= self.bet_fraction <= 1:
            errs.append("押注比例必須在 0~1 之間(0% ~ 100%)")
        if self.interval_min < 0 or self.interval_max < 0:
            errs.append("下注間距不可為負")
        if self.interval_max < self.interval_min:
            errs.append("最大間距必須 ≥ 最小間距")
        if self.strategy not in ("auto", "fixed", "kelly"):
            errs.append(f"策略必須是 auto / fixed / kelly,目前 {self.strategy!r}")
        if self.goal < 0:
            errs.append("目標餘額不可為負")
        if self.goal_action not in ("pause", "raise"):
            errs.append("達標行為必須是 pause 或 raise")
        if self.goal_step < 0:
            errs.append("raise 步進不可為負")
        if self.loss_floor < 0:
            errs.append("停損點不可為負")
        if self.loss_action not in ("pause", "lower_threshold"):
            errs.append("停損行為必須是 pause 或 lower_threshold")
        if self.loss_floor > 0 and self.threshold > 0 and self.loss_floor >= self.threshold:
            errs.append(f"停損點({self.loss_floor:,})應低於保底門檻({self.threshold:,})")
        if self.loss_streak_pause < 0:
            errs.append("連敗冷靜場數不可為負")
        if self.loss_streak_cooldown_min < 0:
            errs.append("冷靜分鐘不可為負")
        if self.bigwin_multiplier < 1.0:
            errs.append("中大獎賠率門檻必須 ≥ 1.0x")
        if self.notify_user_id and not self.notify_user_id.isdigit():
            errs.append("通知對象 User ID 必須是純數字(Discord ID)")
        # 進階策略驗證
        if self.hourly_min_bets < 1:
            errs.append("hourly_min_bets 必須 ≥ 1")
        if not 0.0 <= self.hourly_min_winrate <= 1.0:
            errs.append("hourly_min_winrate 必須在 0~1")
        if self.hourly_min_ev < 0:
            errs.append("hourly_min_ev 不可為負")
        if self.rolling_window_size < 10:
            errs.append("rolling_window_size 必須 ≥ 10")
        if self.rolling_low_ev > self.rolling_high_ev:
            errs.append("rolling_low_ev 必須 ≤ rolling_high_ev")
        if not 0 <= self.rolling_low_mult <= 5:
            errs.append("rolling_low_mult 必須在 0~5")
        if not 0 <= self.rolling_high_mult <= 5:
            errs.append("rolling_high_mult 必須在 0~5")
        if not 0 < self.trailing_stop_pct <= 100:
            errs.append("trailing_stop_pct 必須在 0~100")
        if self.trailing_stop_cooldown_min < 0:
            errs.append("trailing_stop_cooldown_min 不可為負")
        return errs


@dataclass
class EmailConfig:
    enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    user: str = ""
    password: str = ""                 # 加密存到 secrets,不會放在 config payload
    to: str = ""
    notify_goal: bool = True
    notify_loss: bool = True
    notify_bigwin: bool = True
    notify_dead: bool = True
    notify_neko: bool = True
    notify_digest: bool = True
    digest_hour: int = DEFAULT_DIGEST_HOUR
    dead_threshold: int = DEFAULT_DEAD_THRESHOLD

    def validate(self) -> list[str]:
        errs: list[str] = []
        if not 1 <= self.smtp_port <= 65535:
            errs.append(f"SMTP port 必須在 1~65535,目前 {self.smtp_port}")
        if not 0 <= self.digest_hour <= 23:
            errs.append("摘要時段必須在 0~23 整點")
        if self.dead_threshold < 1:
            errs.append("停擺門檻必須 ≥ 1")
        if self.enabled:
            if not self.user:
                errs.append("Email 啟用但未設定寄件人")
            if not self.to:
                errs.append("Email 啟用但未設定收件人")
            # password 不在 dataclass 裡驗證(因為從 secrets 讀,可能被 mask 成 ***)
            if "@" in self.user and " " in self.user:
                errs.append("寄件人格式異常(含空白)")
        return errs


@dataclass
class NekomusumeConfig:
    enabled: bool = True
    check_interval_min: float = float(DEFAULT_NEKO_INTERVAL_MIN)
    auto_claim: bool = False

    def validate(self) -> list[str]:
        errs: list[str] = []
        if self.check_interval_min < 1:
            errs.append("檢查間距必須 ≥ 1 分鐘")
        return errs


@dataclass
class TransferConfig:
    enabled: bool = False
    target: str = ""
    amount: int = 100
    interval_min: float = float(DEFAULT_TRANSFER_INTERVAL_MIN)

    def validate(self) -> list[str]:
        errs: list[str] = []
        if self.amount < 0:
            errs.append("轉帳金額不可為負")
        if self.interval_min < 1:
            errs.append("轉帳間距必須 ≥ 1 分鐘")
        if self.enabled:
            if not self.target.strip():
                errs.append("自動轉帳啟用但未設定對象")
            if self.amount <= 0:
                errs.append("自動轉帳啟用但金額為 0")
        return errs


@dataclass
class DashboardConfig:
    enabled: bool = True
    host: str = DEFAULT_DASHBOARD_HOST
    port: int = DEFAULT_DASHBOARD_PORT
    username: str = DEFAULT_DASHBOARD_USERNAME
    password: str = ""                 # 加密存到 secrets

    def validate(self) -> list[str]:
        errs: list[str] = []
        if not 1 <= self.port <= 65535:
            errs.append(f"Dashboard port 必須在 1~65535,目前 {self.port}")
        if self.host not in ("0.0.0.0", "127.0.0.1", "localhost") \
                and not _looks_like_ip(self.host):
            errs.append(f"監聽位址格式異常: {self.host}")
        if not self.username.strip():
            errs.append("Dashboard 帳號不可為空")
        return errs

    def is_lan_open(self) -> bool:
        """是否會開放給 LAN(0.0.0.0 + 有設密碼才安全)。"""
        return self.host == "0.0.0.0"


@dataclass
class StockConfig:
    """股票監視 + 建議。Phase 1-2:純建議,不會自動買賣。

    工作流程:
    1. 每 poll_interval_min 分鐘:跑 /portfolio,抓所有持股 + 現價
    2. 對 tracked_symbols(使用者想觀察但沒持有的)各跑一次
       /stock symbol:XXX 抓現價
    3. 把所有價格存進 stock_prices DB,做技術分析建議
    """
    enabled: bool = False
    poll_interval_min: float = 15.0   # 多久抓一次價格(分鐘)
    # 指令名稱(可調,通常不用改)
    portfolio_command: str = "/portfolio"   # 查所有持股
    stock_command:     str = "/stock"       # 查單一股票(需 symbol param)
    # 觀察清單:這些 symbol 即使沒持有也會抓價,讓 buy signal 能評估
    # 例:["HOLO", "MAID", "SEGA"]
    tracked_symbols: list[str] = field(default_factory=list)
    # 分析參數(預設值對中等波動股已適用,不熟可不調)
    ma_short: int   = 5      # 短均線(看近期趨勢):5 筆 = 75 分(15min × 5)
    ma_long:  int   = 20     # 長均線(看大方向):20 筆 = 5 小時
    take_profit_pct: float = 15.0    # 持股獲利達此 % → 建議賣(獲利了結)
    stop_loss_pct:   float = 10.0    # 持股虧損達此 % → 建議賣(止損)
    signal_score_threshold: int = 80     # 評分 ≥ 此值才視為「強訊號」
    # 不再有 log_raw_text — parser 失敗時會自動寫到 logs/stock_debug.log

    def validate(self) -> list[str]:
        errs: list[str] = []
        if self.poll_interval_min < 1:
            errs.append("poll_interval_min 必須 ≥ 1")
        if self.ma_short >= self.ma_long:
            errs.append(f"ma_short ({self.ma_short}) 必須 < ma_long ({self.ma_long})")
        if not 0 < self.take_profit_pct <= 1000:
            errs.append("take_profit_pct 必須在 0~1000")
        if not 0 < self.stop_loss_pct <= 100:
            errs.append("stop_loss_pct 必須在 0~100")
        if not 0 <= self.signal_score_threshold <= 100:
            errs.append("signal_score_threshold 必須在 0~100")
        # 衛生:tracked_symbols 應為大寫字母+數字
        for s in self.tracked_symbols:
            if not (isinstance(s, str) and s and s.upper() == s
                    and all(c.isalnum() for c in s)):
                errs.append(f"tracked_symbols 含異常 symbol: {s!r}")
        return errs


@dataclass
class UpdaterConfig:
    """GitHub 版本檢查 / 自動更新。"""
    auto_check: bool = True              # 開機後自動定期檢查新版
    check_interval_min: int = 60         # 多久檢查一次(分鐘)
    auto_update: bool = False            # 偵測到新版 → 自動 git pull + reboot
    branch: str = "main"

    def validate(self) -> list[str]:
        errs: list[str] = []
        if self.check_interval_min < 5:
            errs.append("檢查間距必須 ≥ 5 分鐘(避免 GitHub 限流)")
        if not self.branch.replace("-", "").replace("_", "").replace("/", "").isalnum():
            errs.append(f"branch 名稱含異常字元: {self.branch!r}")
        return errs


@dataclass
class BotConfig:
    """整份設定。除了上述 section,還有頂層欄位 guild_id / channel_id / log_level。"""
    guild_id: str = ""
    channel_id: str = ""
    log_level: str = "INFO"
    gambling: GamblingConfig = field(default_factory=GamblingConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    nekomusume: NekomusumeConfig = field(default_factory=NekomusumeConfig)
    transfer: TransferConfig = field(default_factory=TransferConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    updater: UpdaterConfig = field(default_factory=UpdaterConfig)
    stock: StockConfig = field(default_factory=StockConfig)

    def validate(self) -> list[str]:
        errs: list[str] = []
        if not self.guild_id.isdigit():
            errs.append("guild_id 必須是純數字")
        if not self.channel_id.isdigit():
            errs.append("channel_id 必須是純數字")
        if self.log_level.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            errs.append(f"log_level 必須是 DEBUG/INFO/WARNING/ERROR,目前 {self.log_level!r}")
        for section in (self.gambling, self.email, self.nekomusume,
                        self.transfer, self.dashboard, self.updater, self.stock):
            errs.extend(section.validate())
        return errs

    def to_dict(self) -> dict:
        """完整 dict(含敏感欄位)— 內部用。儲存到 DB 時要用 to_persistable() 拆掉敏感欄位。"""
        return asdict(self)

    def to_redacted_dict(self) -> dict:
        """給 dashboard /api/config 用 — 敏感欄位遮成 *** 或空字串。"""
        d = asdict(self)
        for section, field_name in SENSITIVE_FIELDS:
            if section in d and field_name in d[section]:
                v = d[section][field_name]
                d[section][field_name] = "***" if v else ""
        return d


# ── 驗證 helpers ──────────────────────────────────────────────────────
def _looks_like_ip(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit() or not 0 <= int(p) <= 255:
            return False
    return True


# ── DB 持久化 ────────────────────────────────────────────────────────
async def load_config(db) -> BotConfig:
    """從 DB 載入 BotConfig。若任何 section 缺失,用預設值補。"""
    raw = await db.get_all_config()

    cfg = BotConfig(
        guild_id=raw.get("_meta", {}).get("guild_id", ""),
        channel_id=raw.get("_meta", {}).get("channel_id", ""),
        log_level=raw.get("_meta", {}).get("log_level", "INFO"),
    )
    cfg.gambling   = _build_dc(GamblingConfig,   raw.get("gambling", {}))
    cfg.email      = _build_dc(EmailConfig,      raw.get("email", {}))
    cfg.nekomusume = _build_dc(NekomusumeConfig, raw.get("nekomusume", {}))
    cfg.transfer   = _build_dc(TransferConfig,   raw.get("transfer", {}))
    cfg.dashboard  = _build_dc(DashboardConfig,  raw.get("dashboard", {}))
    cfg.updater    = _build_dc(UpdaterConfig,    raw.get("updater", {}))
    cfg.stock      = _build_dc(StockConfig,      raw.get("stock", {}))

    # 從 secrets table 補回敏感欄位的明文(讓記憶體中的 cfg 物件能直接用)
    cfg.email.password = await db.get_secret("email_password")
    cfg.dashboard.password = await db.get_secret("dashboard_password")

    return cfg


def _build_dc(cls, data: dict):
    """從 dict 建 dataclass 實例;只取 dataclass 認識的 key,其他丟掉。"""
    valid_keys = {f.name for f in fields(cls)}
    cleaned = {k: v for k, v in data.items() if k in valid_keys}
    try:
        return cls(**cleaned)
    except (TypeError, ValueError) as e:
        log.warning("無法建立 %s,改用預設值: %s", cls.__name__, e)
        return cls()


async def save_config(db, config: BotConfig) -> list[str]:
    """把 BotConfig 寫到 DB。回傳驗證錯誤清單(若有)。即便有錯仍會儲存可儲存的部分。

    敏感欄位走 secrets table(加密),其他走 config table(明文 JSON)。
    """
    errs = config.validate()

    # _meta(guild_id / channel_id / log_level)
    await db.set_config_section("_meta", {
        "guild_id":  config.guild_id,
        "channel_id": config.channel_id,
        "log_level": config.log_level,
    })

    # 各 section,但敏感欄位要拿掉
    for section_name, section_obj in [
        ("gambling",   config.gambling),
        ("email",      config.email),
        ("nekomusume", config.nekomusume),
        ("transfer",   config.transfer),
        ("dashboard",  config.dashboard),
        ("updater",    config.updater),
        ("stock",      config.stock),
    ]:
        d = asdict(section_obj)
        for sn, fn in SENSITIVE_FIELDS:
            if sn == section_name and fn in d:
                d.pop(fn)
        await db.set_config_section(section_name, d)

    # 敏感欄位 → secrets
    await db.set_secret("email_password", config.email.password or "")
    await db.set_secret("dashboard_password", config.dashboard.password or "")

    return errs


def merge_partial(config: BotConfig, partial: dict) -> list[str]:
    """從 dashboard POST 的部分更新 dict 套到 config(in-place);回傳錯誤訊息。

    partial 的格式跟 to_redacted_dict() 同 — 例如 {"gambling": {"min_bet": 100, ...}}
    任何 key 為 None 的欄位會略過(視為「保留現值」)。
    敏感欄位若是 "***" 或空字串(且原本有值)也視為「保留現值」。
    """
    if not isinstance(partial, dict):
        return ["payload 必須是 dict"]

    # 頂層欄位
    for fld in ("guild_id", "channel_id", "log_level"):
        if fld in partial and partial[fld] is not None:
            setattr(config, fld, str(partial[fld]))

    section_map = {
        "gambling":   config.gambling,
        "email":      config.email,
        "nekomusume": config.nekomusume,
        "transfer":   config.transfer,
        "dashboard":  config.dashboard,
        "updater":    config.updater,
        "stock":      config.stock,
    }
    for section_name, section_obj in section_map.items():
        section_data = partial.get(section_name)
        if not isinstance(section_data, dict):
            continue
        valid = {f.name: f.type for f in fields(section_obj.__class__)}
        for k, v in section_data.items():
            if k not in valid:
                continue
            if v is None:
                continue
            # 敏感欄位:"***" 或空字串視為「保留現值」
            if (section_name, k) in SENSITIVE_FIELDS:
                if v == "***" or v == "":
                    continue
            try:
                # 簡單型別轉換(從 JSON 來的可能是字串)
                cur = getattr(section_obj, k)
                if isinstance(cur, bool):
                    if isinstance(v, str):
                        v = v.lower() in ("true", "1", "yes", "y", "on")
                    else:
                        v = bool(v)
                elif isinstance(cur, int) and not isinstance(cur, bool):
                    v = int(v)
                elif isinstance(cur, float):
                    v = float(v)
                else:
                    v = str(v)
                setattr(section_obj, k, v)
            except (TypeError, ValueError) as e:
                log.warning("merge_partial: 欄位 %s.%s 無法轉型(%s): %s",
                            section_name, k, v, e)

    return config.validate()


# ── 一次性遷移:從 config.json → DB ────────────────────────────────────
async def migrate_from_json_if_needed(db, json_path: str) -> bool:
    """若 DB 還沒有 config 資料,且 config.json 存在,做一次性遷移。

    遷移完成後不會刪除原 JSON(讓使用者保留備份);只在 meta 寫入
    "json_migrated_at" 防止重複遷移。回傳是否做了遷移。
    """
    import json
    import os

    already = await db.get_meta("json_migrated_at")
    if already:
        return False
    if not os.path.exists(json_path):
        return False

    try:
        with open(json_path, encoding="utf-8") as f:
            old = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("讀 %s 失敗,跳過遷移: %s", json_path, e)
        return False

    # 把 old dict 套到一個全新的 BotConfig
    cfg = BotConfig()
    cfg.guild_id   = str(old.get("guild_id") or "")
    cfg.channel_id = str(old.get("channel_id") or "")
    cfg.log_level  = old.get("log_level", "INFO")

    # 移除 placeholder 殘留
    for fld in ("guild_id", "channel_id"):
        v = getattr(cfg, fld)
        if "YOUR_" in v or "HERE" in v:
            setattr(cfg, fld, "")

    # 處理 placeholder notify_user_id
    raw_uid = (old.get("gambling") or {}).get("notify_user_id", "")
    if raw_uid and "YOUR_" not in str(raw_uid) and "HERE" not in str(raw_uid):
        cfg.gambling = _build_dc(GamblingConfig, old.get("gambling", {}))
    else:
        gd = dict(old.get("gambling", {}))
        gd["notify_user_id"] = ""
        cfg.gambling = _build_dc(GamblingConfig, gd)

    cfg.email      = _build_dc(EmailConfig,      old.get("email", {}))
    cfg.nekomusume = _build_dc(NekomusumeConfig, old.get("nekomusume", {}))
    cfg.transfer   = _build_dc(TransferConfig,   old.get("transfer", {}))
    cfg.dashboard  = _build_dc(DashboardConfig,  old.get("dashboard", {}))
    cfg.updater    = _build_dc(UpdaterConfig,    old.get("updater", {}))

    # 安全預設:若 dashboard 沒密碼且是 0.0.0.0 → 退到 127.0.0.1
    if cfg.dashboard.enabled and not (cfg.dashboard.password or "").strip():
        if cfg.dashboard.host == "0.0.0.0":
            log.warning("遷移時偵測 dashboard 無密碼但綁 0.0.0.0,強制改為 127.0.0.1")
            cfg.dashboard.host = "127.0.0.1"

    await save_config(db, cfg)
    await db.set_meta("json_migrated_at", str(__import__("time").time()))
    log.info("已從 %s 遷移設定到 DB", json_path)
    return True


async def migrate_history_from_json(db, history_path: str) -> int:
    """一次性把舊 gambling_history.json 寫進 DB。回傳遷移筆數。"""
    import json
    import os

    if not os.path.exists(history_path):
        return 0
    already = await db.get_meta("history_migrated_at")
    if already:
        return 0
    try:
        with open(history_path, encoding="utf-8") as f:
            old = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("讀 %s 失敗: %s", history_path, e)
        return 0
    if not isinstance(old, list):
        return 0
    n = await db.bulk_import_history(old)
    await db.set_meta("history_migrated_at", str(__import__("time").time()))
    log.info("已遷移 %d 筆下注紀錄到 DB", n)
    return n


async def migrate_analysis_from_json(db, analysis_path: str) -> bool:
    import json
    import os

    if not os.path.exists(analysis_path):
        return False
    already = await db.get_meta("analysis_migrated_at")
    if already:
        return False
    try:
        with open(analysis_path, encoding="utf-8") as f:
            sa = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning("讀 %s 失敗: %s", analysis_path, e)
        return False
    if not isinstance(sa, dict):
        return False
    await db.save_slot_analysis(sa)
    await db.set_meta("analysis_migrated_at", str(__import__("time").time()))
    log.info("已遷移 slot_analysis 到 DB")
    return True


# ── 取得單一欄位(避免每次都要讀整份 config) ───────────────────────────
def get_value(config: BotConfig, dotted: str, default: Any = None) -> Any:
    """以 'gambling.threshold' 形式取值;不存在回 default。"""
    parts = dotted.split(".")
    obj: Any = config
    for p in parts:
        if hasattr(obj, p):
            obj = getattr(obj, p)
        elif isinstance(obj, dict) and p in obj:
            obj = obj[p]
        else:
            return default
    return obj

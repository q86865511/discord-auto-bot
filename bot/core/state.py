"""共用 runtime 狀態:thread/coroutine 安全的單一來源。

替代原本散在 main.py 的 `state: dict`。

設計原則:
- 用 dataclass 給型別明確的結構,但仍允許動態欄位(透過 `extras` 做為逃生口)
- 所有「讀-改-寫」操作走 `BotState.update()`(包在 asyncio.Lock 中)
- UI / dashboard 讀取走 `snapshot()`(回傳 deep copy,避免並發改動)
- 鍵盤 listener 等執行緒會用 thread-safe `threading.Lock` 寫 pending_key

對外快速 API:
    state = BotState()
    async with state.lock:
        state.balance = 1000
        state.wins += 1
    snap = await state.snapshot()
    state.queue_log("...")  # 給 UI 面板看
"""
from __future__ import annotations

import asyncio
import copy
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .constants import UI_LOG_LINES_MAX


@dataclass
class EventCounters:
    hourly_claims:   int = 0
    daily_claims:    int = 0
    transfers:       int = 0
    neko_completes:  int = 0
    stop_loss_fires: int = 0
    goal_hits:       int = 0
    bigwins:         int = 0
    since_ts:        float = field(default_factory=time.time)


@dataclass
class BotState:
    """整個 bot runtime 的可變狀態。

    重要 invariant:
    - 所有「修改」(包含 `+=`、修改子 dict 內容、append history…)都應在
      `async with state.lock:` 區段內進行。
    - UI / dashboard 讀取要走 `snapshot()`(deep copy)。
    """

    # ── 餘額 / 統計 ─────────────────────────────────────────────────
    balance: int | None = None
    start_balance: int | None = None
    total_bets: int = 0
    wins: int = 0
    losses: int = 0
    net_change: int = 0
    current_bet: int = 0

    # ── 排程倒數 ─────────────────────────────────────────────────────
    hourly_next: float | None = None
    daily_next:  float | None = None

    # ── 控制旗標 ─────────────────────────────────────────────────────
    quit:    bool = False
    reboot:  bool = False
    paused:  bool = False
    status:  str  = "初始化中"

    # ── Streak / cooldown ───────────────────────────────────────────
    current_streak: int = 0           # >0 連勝;<0 連敗
    max_win_streak: int = 0
    max_loss_streak: int = 0
    cooldown_until_ts: float | None = None

    # ── 目標 / 停損 ─────────────────────────────────────────────────
    goal_reached:   bool = False
    loss_triggered: bool = False

    # ── 貓娘 ────────────────────────────────────────────────────────
    neko_status:        str = "unknown"      # dispatching / not_dispatching / unknown
    neko_deadline_ts:   float | None = None
    neko_last_check_ts: float | None = None
    neko_check_ts:      float | None = None

    # ── 連線健康 ─────────────────────────────────────────────────────
    dead_notified:        bool = False
    recover_fail_streak:  int  = 0

    # ── 版本檢查(updater_loop 維護) ──────────────────────────────
    update_available:    bool = False
    local_commit:        str | None = None    # full SHA;UI 顯示時截 [:7]
    remote_commit:       str | None = None
    last_update_check:   float | None = None

    # ── 股票 runtime 狀態(stock_loop 維護) ──────────────────────
    # 最近一次 poll 的快照:{ts, prices, holdings, signals}
    stock_last_snapshot: dict = field(default_factory=dict)
    stock_last_poll_ts:  float | None = None

    # ── 進階策略 runtime 狀態 ──────────────────────────────────────
    # Trailing stop 冷卻:用 epoch 秒數,跟 cooldown_until_ts 同模式
    trailing_cooldown_until_ts: float | None = None
    # 觸發後 baseline 重設成當下的 history 長度;之後的 drawdown 只算此之後
    # 這個機制避免冷卻結束時 peak 還是歷史最高 → 立刻又觸發 → 死循環
    trailing_baseline_idx: int = 0
    # 統計顯示用
    strategy_skipped_hourly:  int = 0    # hourly filter 累計跳過數
    strategy_skipped_trailing:int = 0
    strategy_trailing_triggers: int = 0
    strategy_recent_ev_mult:  float = 1.0  # 最近一次 rolling 倍率(0/1.x)

    # ── 識別 ────────────────────────────────────────────────────────
    guild_id:   str = ""
    channel_id: str = ""
    session_start_ts: float = field(default_factory=time.time)

    # ── 滑動視窗(讓 UI / dashboard 看最近) ────────────────────────
    log_lines: deque[str] = field(default_factory=lambda: deque(maxlen=UI_LOG_LINES_MAX))

    # ── 計數器 ──────────────────────────────────────────────────────
    events: EventCounters = field(default_factory=EventCounters)

    # ── 累計分析 / 歷史(由 DB 維護,記憶體鏡像加快讀取) ──────────
    slot_analysis: dict = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)

    # ── 跨 thread 鍵盤輸入 ──────────────────────────────────────────
    _kb_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _pending_key: str | None = None

    # ── async lock(保護 state 修改) ──────────────────────────────
    _async_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def lock(self) -> asyncio.Lock:
        """async lock — 修改 state 前後 `async with state.lock:`。"""
        return self._async_lock

    # ── 鍵盤 listener 用的 thread-safe API ──────────────────────────
    def push_key(self, key: str) -> None:
        """從 thread(鍵盤監聽)寫入 pending key。"""
        with self._kb_lock:
            self._pending_key = key

    def pop_key(self) -> str | None:
        """從 async loop 取出 pending key 並清除。"""
        with self._kb_lock:
            k = self._pending_key
            self._pending_key = None
        return k

    # ── 日誌追加(thread-safe;UI 顯示用) ────────────────────────────
    def queue_log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_lines.append(f"{ts} {msg}")

    def reset_event_counters(self) -> None:
        """每日摘要寄出後 reset 區間計數器(history/slot_analysis 不重置)。"""
        self.events = EventCounters()

    # ── Snapshot:給 UI / dashboard 用的不可變副本 ──────────────────
    def snapshot(self) -> dict:
        """以 dict 形式返回當前 state(deep copy)。

        注意:讀取本身就是非原子的(deque 在 iter 期間不能被改),建議
        callers 仍在 async with state.lock 區段內呼叫,或接受偶爾不一致。
        """
        d = {
            "balance":             self.balance,
            "start_balance":       self.start_balance,
            "total_bets":          self.total_bets,
            "wins":                self.wins,
            "losses":              self.losses,
            "net_change":          self.net_change,
            "current_bet":         self.current_bet,
            "hourly_next":         self.hourly_next,
            "daily_next":          self.daily_next,
            "quit":                self.quit,
            "reboot":              self.reboot,
            "paused":              self.paused,
            "status":              self.status,
            "current_streak":      self.current_streak,
            "max_win_streak":      self.max_win_streak,
            "max_loss_streak":     self.max_loss_streak,
            "cooldown_until_ts":   self.cooldown_until_ts,
            "goal_reached":        self.goal_reached,
            "loss_triggered":      self.loss_triggered,
            "neko_status":         self.neko_status,
            "neko_deadline_ts":    self.neko_deadline_ts,
            "neko_last_check_ts":  self.neko_last_check_ts,
            "neko_check_ts":       self.neko_check_ts,
            "dead_notified":       self.dead_notified,
            "recover_fail_streak": self.recover_fail_streak,
            "update_available":    self.update_available,
            "local_commit":        self.local_commit,
            "remote_commit":       self.remote_commit,
            "last_update_check":   self.last_update_check,
            "guild_id":            self.guild_id,
            "channel_id":          self.channel_id,
            "session_start_ts":    self.session_start_ts,
            "log_lines":           list(self.log_lines),
            "events":              copy.deepcopy(self.events.__dict__),
            "slot_analysis":       copy.deepcopy(self.slot_analysis),
            "history":             list(self.history),
        }
        return d


# ── 暫停 / 中斷可恢復的睡眠 ─────────────────────────────────────────
async def interruptible_sleep(state: BotState, seconds: float) -> None:
    """0.5 秒分段睡眠,遇 quit 立即結束、遇 paused 則停留。"""
    deadline = time.time() + seconds
    while not state.quit:
        if state.paused:
            await asyncio.sleep(0.5)
            deadline += 0.5
            continue
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.5, remaining))


async def wait_while_paused(state: BotState) -> None:
    """暫停時原地等待。"""
    while state.paused and not state.quit:
        await asyncio.sleep(0.5)

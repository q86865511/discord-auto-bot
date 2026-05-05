"""
Discord 自動指令腳本 - 含 Rich 終端 UI
"""
import asyncio
import csv
import json
import logging
import logging.handlers
import msvcrt
import os
import random
import re
import smtplib
import sys
import threading
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText

from playwright.async_api import async_playwright, Page
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── 模組拆分後的 import（package: bot/）─────────────────────────────────────
# slot embed 解析（regex / DOM 文字擷取）
from bot.slot.parsers import (
    SLOT_WIN_PATTERN, SLOT_LOSS_PATTERN,
    SLOT_LINE_PATTERN, SLOT_RESULT_BLOCK, SLOT_FULL_BLOCK,
    _AUX_EMOJIS, _NON_SLOT_SHORTCODES,
    _parse_balance_int, _parse_slot_change,
    _parse_slot_lines, _parse_slot_grid,
    _get_page_text, _debug_dump_slot_text,
)

# Slot 分析資料模型 / 計算 / 持久化 / 顯示工具
from bot.slot.analysis import (
    MIN_KELLY_SAMPLES,
    PAYOUT_BUCKETS, HIGH_MULT_THRESHOLD, HIGH_MULT_KEEP,
    ANALYSIS_PATH, HISTORY_PATH, HISTORY_MAX_LEN,
    _SYMBOL_DISPLAY_THRESHOLD,
    _SHORTCODE_EMOJI_MAP,
    _make_slot_analysis, _update_slot_analysis, compute_slot_stats,
    compute_hourly_breakdown,
    _format_symbol_display, _is_noise_symbol,
    load_slot_analysis, save_slot_analysis,
    load_history, save_history,
)

# ── 常數 ──────────────────────────────────────────────────────────────────────
CONFIG_PATH = "config.json"
STORAGE_STATE_PATH = "storage_state.json"
EXPORT_DIR = "exports"
LOG_FILE_PATH = "bot.log"
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024   # 5MB / 個
LOG_FILE_BACKUP_COUNT = 3              # 保留 3 個輪替檔（bot.log.1 .2 .3）
# /hourly 採「時鐘整點錨定」策略：等到下個整點 :MM:SS（MM=0~3 隨機）才送，
# 避免「上次 13:01 領 → +60min jitter → 14:08」之類的錯位（reset 在 :00），
# 也不會因為跑得比一小時快而連續送兩次（領取週期由真實 hour boundary 決定）。
HOURLY_POST_BOUNDARY_MIN_SEC =  30   # 過了整點後最快多久送
HOURLY_POST_BOUNDARY_MAX_SEC = 180   # 過了整點後最慢多久送（隨機在 [min,max] 之間）
DAILY_BASE_SEC    = 86400
DAILY_JITTER_SEC  = 45 * 60
DAILY_STARTUP_DELAY_SEC = 300
TYPING_DELAY_MIN_MS = 50
TYPING_DELAY_MAX_MS = 150
GAMBLE_RECHECK_SEC  = 300
DEFAULT_NOTIFY_USER_ID = "429881182168023040"
DEFAULT_INTERVAL_MIN = 4
DEFAULT_INTERVAL_MAX = 10
DEFAULT_GOAL = 0          # 0 = 未設定目標
DEFAULT_GOAL_ACTION = "pause"   # "pause" 或 "raise"
DEFAULT_GOAL_STEP   = 10000     # raise 模式：新目標 = 舊目標 + step
DEFAULT_LOSS_FLOOR  = 0         # 0 = 未設定停損
DEFAULT_LOSS_ACTION = "pause"   # "pause" 或 "lower_threshold"
DEFAULT_LOSS_STEP   = 5000      # lower_threshold 模式：新門檻 = 當前餘額 - step
DEFAULT_NEKOMUSUME_INTERVAL_MIN = 30   # /check 監控間距
DEFAULT_BIGWIN_MULTIPLIER = 5.0  # 中大獎賠率門檻（總計贏得 / 下注）
DEFAULT_DEAD_THRESHOLD    = 2    # 連續讀取餘額失敗幾次算「bot 停擺」
REBOOT_EXIT_CODE = 42     # main 退出時用這個碼通知 run.bat 重新啟動

# 自動轉帳預設值
DEFAULT_TRANSFER_INTERVAL_MIN = 60   # 預設每 60 分鐘轉一次

# 每日 email 摘要的觸發小時（24h 制；00:00 = 隔日寄前一日 24h 摘要）
DEFAULT_DIGEST_HOUR = 0

command_lock = asyncio.Lock()
console = Console()


def make_state() -> dict:
    return {
        "balance":      None,
        "start_balance": None,
        "total_bets":   0,
        "wins":         0,
        "losses":       0,
        "net_change":   0,
        "current_bet":  0,
        "status":       "初始化中",
        "log_lines":    [],
        "hourly_next":  None,
        "daily_next":   None,
        "quit":         False,
        "reboot":       False,
        "paused":       False,
        "pending_key":  None,
        "history":      [],   # 每筆 {ts, bet, before, after, change, result}
        "goal_reached": False,
        "loss_triggered": False,    # 停損是否已觸發（避免一直 spam 通知）
        # Streak 追蹤：current 正數 = 連勝、負數 = 連敗、0 = 沒紀錄；
        # max_win/max_loss 紀錄歷史最高（不會重置）；cooldown_until_ts 是連敗
        # 觸發冷靜時段時設定的解封時間
        "current_streak": 0,
        "max_win_streak": 0,
        "max_loss_streak": 0,
        "cooldown_until_ts": None,
        "neko_status": "unknown",     # dispatching / not_dispatching / unknown
        "neko_deadline_ts": None,     # 派遣完成時間戳；用來本地倒數，避免一直 /check
        "neko_last_check_ts": None,
        "neko_check_ts":     None,  # 上次 /check 讀到剩餘時間的 epoch；用於 UI 即時倒數
        "dead_notified":    False,  # 「bot 停擺」email 是否已寄出，避免重複通知
        "recover_fail_streak": 0,   # recover_page 連續失敗次數；累積到門檻就觸發 browser 重啟
        "session_start_ts": time.time(),
        # 事件累計（給每日摘要用；reset 在每次寄出後）
        "events": {
            "hourly_claims":   0,
            "daily_claims":    0,
            "transfers":       0,    # 轉帳成功次數
            "neko_completes":  0,    # 偵測到貓娘完成次數
            "stop_loss_fires": 0,    # 停損觸發次數
            "goal_hits":       0,    # 達標次數
            "bigwins":         0,    # 中大獎次數
            "since_ts":        time.time(),   # 此摘要區間起始
        },
        "slot_analysis": _make_slot_analysis(),
    }


# ── 日誌 ──────────────────────────────────────────────────────────────────────
class UILogHandler(logging.Handler):
    def __init__(self, state: dict):
        super().__init__()
        self.state = state
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        try:
            self.state["log_lines"].append(self.format(record))
            if len(self.state["log_lines"]) > 25:
                self.state["log_lines"].pop(0)
        except Exception:
            self.handleError(record)


def setup_logging(state: dict, log_level: str = "INFO") -> logging.Logger:
    """
    設定統一的 logging：
      1. UILogHandler — 將 INFO+ 訊息推到 state["log_lines"]，給 UI 面板顯示
      2. RotatingFileHandler — 將 DEBUG+ 訊息寫到 bot.log（最多 5MB×3 個檔案）
    第三方套件（playwright / asyncio / urllib3 等）的 noise 拉到 WARNING 以上。
    """
    level_name = (log_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)   # root 最寬，handler 各自決定要不要過

    # UI handler — 走 INFO 以上（避免 DEBUG 洗版）
    ui = UILogHandler(state)
    ui.setLevel(logging.INFO)
    root.addHandler(ui)

    # File handler — 走使用者設定的 level（預設 INFO；可調 DEBUG）
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE_PATH,
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setLevel(level)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(fh)
    except OSError as e:
        # 寫不了檔（權限／磁碟滿）— 不要因此整個 bot 不能啟動
        print(f"⚠ 無法開啟 {LOG_FILE_PATH}（{e}），只用 UI 日誌")

    # 把第三方 module 的 noise 拉到 WARNING 以上
    for noisy in ("urllib3", "asyncio", "playwright", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger(__name__)


# ── 暫停 / 中斷可恢復的睡眠 ─────────────────────────────────────────────────
async def interruptible_sleep(state: dict, seconds: float):
    """
    用 0.5 秒分段睡眠，遇到 quit 立即結束、遇到 paused 則停留直到恢復。
    讓長時間 sleep（hourly 1 小時、daily 24 小時）能被「P」鍵即時打斷。
    """
    deadline = time.time() + seconds
    while not state["quit"]:
        if state.get("paused"):
            await asyncio.sleep(0.5)
            deadline += 0.5   # 暫停期間不消耗 sleep 預算
            continue
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.5, remaining))


# ── 賭博紀錄匯出 ─────────────────────────────────────────────────────────────
def _export_filename(prefix: str, ext: str) -> str:
    os.makedirs(EXPORT_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(EXPORT_DIR, f"{prefix}_{ts}.{ext}")


def export_history_csv(state: dict) -> str | None:
    history = state.get("history") or []
    if not history:
        return None
    path = _export_filename("gambling", "csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["時間", "下注", "下注前餘額", "下注後餘額", "變動", "結果", "中獎線路"])
        for r in history:
            lines_json = (json.dumps(r["lines"], ensure_ascii=False)
                          if r.get("lines") else "")
            writer.writerow([
                r["ts"], r["bet"], r["before"], r["after"],
                r["change"], r["result"], lines_json,
            ])
    return path


def export_history_chart(state: dict) -> str | None:
    """畫餘額變化折線圖；matplotlib 沒裝就回 None（不影響 CSV 匯出）。"""
    history = state.get("history") or []
    if not history:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    xs = list(range(1, len(history) + 1))
    balances = [r["after"] for r in history]
    nets = []
    cum = 0
    for r in history:
        cum += r["change"]
        nets.append(cum)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 7), sharex=True)

    ax1.plot(xs, balances, marker="o", linewidth=1.5, markersize=3, color="#1f77b4")
    ax1.set_ylabel("Balance")
    ax1.set_title(f"Discord Auto Bot - Gambling History ({len(history)} bets)")
    ax1.grid(True, alpha=0.3)

    colors = ["#2ca02c" if r["change"] >= 0 else "#d62728" for r in history]
    ax2.bar(xs, [r["change"] for r in history], color=colors, alpha=0.7,
            label="Per-bet change")
    ax2.plot(xs, nets, color="#ff7f0e", linewidth=2, label="Cumulative net")
    ax2.axhline(y=0, color="gray", linewidth=0.8)
    ax2.set_xlabel("Bet #")
    ax2.set_ylabel("Change")
    ax2.legend(loc="best")
    ax2.grid(True, alpha=0.3)

    path = _export_filename("gambling", "png")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


# （符號顯示工具搬到 slot_analysis 模組；本檔頂端已 from slot_analysis import）


def export_slot_analysis(state: dict) -> str | None:
    sa = state.get("slot_analysis", {})
    if sa.get("total_spins", 0) == 0:
        return None
    stats = compute_slot_stats(sa)
    path = _export_filename("slot_analysis", "txt")
    with open(path, "w", encoding="utf-8") as f:
        n = stats["total_spins"]
        f.write(f"Slot Analysis Report - {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write(f"{'=' * 50}\n\n")
        f.write(f"Total spins:    {n}\n")
        f.write(f"Win rate:       {stats['win_rate']:.2%}\n")
        f.write(f"EV:             {stats['ev']:.4f}x (edge: {stats['edge']:+.2%})\n")
        f.write(f"Std dev:        {stats['std_dev']:.4f}\n")
        f.write(f"Variance:       {stats['variance']:.4f}\n")
        kf = stats["kelly_fraction"]
        f.write(f"Kelly fraction: {kf:.4f} (half: {kf / 2:.4f})\n\n")

        f.write("Payout Distribution\n")
        f.write(f"{'-' * 40}\n")
        dist = stats.get("payout_distribution", {})
        for bucket in PAYOUT_BUCKETS:
            count = int(dist.get(bucket, 0))
            pct = count / n * 100 if n else 0
            f.write(f"  {bucket:>6s}: {count:>5d} ({pct:5.1f}%)\n")
        high_mults = stats.get("high_mults", [])
        if high_mults:
            top = sorted(high_mults, reverse=True)
            f.write(f"  >={int(HIGH_MULT_THRESHOLD)}x actual multipliers ({len(top)}): "
                    + ", ".join(f"{m:.2f}x" for m in top) + "\n")

        si = stats.get("symbol_info", {})
        gp = stats.get("grid_symbol_prob", {})
        if si:
            f.write(f"\nSymbol Stats (from winning lines)\n")
            f.write(f"{'-' * 40}\n")
            f.write(f"  {'Symbol':<14s} {'Hits':>6s} {'AvgMult':>8s} {'TotalPay':>10s}\n")
            for sym, info in sorted(si.items(), key=lambda x: -x[1]["total_payout"]):
                # win_appearances > 0，這裡所有 row 都會通過 noise filter
                disp = _format_symbol_display(sym)
                f.write(f"  {disp:<14s} {info['win_appearances']:>6d} "
                        f"{info['avg_mult']:>8.2f}x {info['total_payout']:>10,}\n")

        if gp:
            f.write(f"\nGrid Symbol Probability\n")
            f.write(f"{'-' * 40}\n")
            hidden = 0
            for sym, prob in sorted(gp.items(), key=lambda x: -x[1]):
                wins = si.get(sym, {}).get("win_appearances", 0)
                if _is_noise_symbol(sym, wins, prob):
                    hidden += 1
                    continue
                disp = _format_symbol_display(sym)
                f.write(f"  {disp:<14s} {prob:>6.1%}\n")
            if hidden > 0:
                f.write(f"  ({hidden} noise symbols hidden: no wins, "
                        f"grid prob < {_SYMBOL_DISPLAY_THRESHOLD:.1%})\n")

        li = stats.get("line_info", {})
        if li:
            f.write(f"\nLine Stats\n")
            f.write(f"{'-' * 40}\n")
            f.write(f"  {'Line':<10s} {'Hits':>6s} {'Rate':>8s} {'TotalPay':>10s}\n")
            for ln, info in sorted(li.items(), key=lambda x: -x[1]["hits"]):
                f.write(f"  {ln:<10s} {info['hits']:>6d} "
                        f"{info['hit_rate']:>7.1%} {info['total_payout']:>10,}\n")
    return path


def _show_history_summary(state: dict):
    """以文字摘要顯示賭博紀錄；圖表由 E 鍵以 PNG 匯出。"""
    history = state.get("history") or []
    if not history:
        return
    cum_net = sum(r.get("change", 0) for r in history)
    last_balance = history[-1].get("after", 0)
    win_count = sum(1 for r in history if r.get("change", 0) > 0)
    lose_count = sum(1 for r in history if r.get("change", 0) < 0)

    console.print()
    console.rule("[bold]📉 賭博紀錄[/]")
    color = "green" if cum_net >= 0 else "red"
    console.print(
        f"  下注次數: [bold]{len(history)}[/]  "
        f"勝/敗: [green]{win_count}[/]/[red]{lose_count}[/]  "
        f"累計淨收: [{color}]{cum_net:+,}[/]  "
        f"目前餘額: [bold]{last_balance:,}[/]"
    )
    console.print(
        "  [dim]（按 [bold]E[/bold] 可匯出 CSV + PNG 折線圖到 exports/）[/dim]"
    )


def _show_slot_analysis(state: dict):
    os.system("cls")
    sa = state.get("slot_analysis", {})
    stats = compute_slot_stats(sa)

    console.print()
    console.rule("[bold cyan]🎰  Slot Machine Analysis[/]")

    if stats.get("total_spins", 0) == 0:
        console.print("\n  [dim]尚無分析資料。開始賭博後會自動累積。[/dim]")
        _show_history_summary(state)
        input("\n  按 Enter 返回...")
        return

    n = stats["total_spins"]
    edge = stats["edge"]
    ec = "green" if edge >= 0 else "red"

    # 基本統計
    bt = Table(box=None, show_header=False, padding=(0, 2))
    bt.add_column(style="dim", no_wrap=True)
    bt.add_column()
    bt.add_row("總旋轉次數", f"{n:,}")
    bt.add_row("勝率",       f"{stats['win_rate']:.1%}")
    bt.add_row(
        "期望值 (EV)",
        f"{stats['ev']:.4f}x  ([{ec}]邊際: {edge:+.2%}[/{ec}])"
    )
    bt.add_row("標準差", f"{stats['std_dev']:.4f}")
    bt.add_row("變異數", f"{stats['variance']:.4f}")
    kf = stats["kelly_fraction"]
    if stats["sufficient_data"]:
        bt.add_row("Kelly f*", f"{kf:.4f}  (半 Kelly: {kf / 2:.4f})")
    else:
        bt.add_row("Kelly f*", f"[dim]資料不足（需 {MIN_KELLY_SAMPLES} 筆，目前 {n}）[/dim]")
    console.print(bt)

    # 賠率分布
    dist = stats.get("payout_distribution", {})
    high_mults = stats.get("high_mults", [])
    console.print()
    console.rule("[bold]📊 賠率分布[/]")
    dt = Table(show_header=True, header_style="bold cyan")
    dt.add_column("區間",     justify="right", no_wrap=True)
    dt.add_column("次數",     justify="right")
    dt.add_column("比例",     justify="right")
    dt.add_column("分布",     justify="left", no_wrap=True)
    dt.add_column("實際賠率", justify="left")  # 只有「以上」桶會用

    for bucket in PAYOUT_BUCKETS:
        count = int(dist.get(bucket, 0))
        pct = count / n * 100 if n else 0
        bar = "█" * min(40, int(pct / 2))
        actual = ""
        if bucket == "以上" and count > 0 and high_mults:
            recent = sorted(high_mults, reverse=True)[:5]
            actual = ", ".join(f"{m:.1f}x" for m in recent)
            if len(high_mults) > len(recent):
                actual += f"  [dim]…+{len(high_mults) - len(recent)}[/dim]"
        dt.add_row(bucket, f"{count:,}", f"{pct:.1f}%", bar, actual)
    console.print(dt)

    # 符號統計
    si = stats.get("symbol_info", {})
    gp = stats.get("grid_symbol_prob", {})
    total_wagered = sa.get("total_wagered", 0) or 1   # 防 div/0
    if si or gp:
        console.print()
        console.rule("[bold]🎯 符號統計  (回收率 = 累計賠付 / 累計下注)[/]")
        st = Table(show_header=True, header_style="bold cyan")
        st.add_column("符號",     justify="left",  no_wrap=True)
        st.add_column("中獎次數", justify="right")
        st.add_column("平均倍率", justify="right")
        st.add_column("累計賠付", justify="right")
        st.add_column("回收率",   justify="right")
        if gp:
            st.add_column("格子機率", justify="right")

        all_symbols = set(si.keys()) | set(gp.keys())

        def _sort_key(s):
            return -(si.get(s, {}).get("total_payout", 0))

        hidden_count = 0
        for sym in sorted(all_symbols, key=_sort_key):
            info = si.get(sym, {})
            wins = info.get("win_appearances", 0)
            prob = gp.get(sym, 0.0)
            # 過濾雜訊：沒中獎 + 格子機率低於閾值（如 :ti: :fh: 這類解析誤觸）
            if _is_noise_symbol(sym, wins, prob):
                hidden_count += 1
                continue
            disp_sym = _format_symbol_display(sym)
            if wins > 0:
                avg_mult = info.get("avg_mult", 0.0)
                total_pay = info.get("total_payout", 0)
                rec_rate = total_pay / total_wagered
                row = [
                    disp_sym,
                    f"{wins:,}",
                    f"{avg_mult:.2f}x",
                    f"{total_pay:,}",
                    f"{rec_rate:.1%}",
                ]
            else:
                row = [disp_sym, "─", "─", "─", "─"]
            if gp:
                row.append(f"{prob:.1%}" if sym in gp else "─")
            st.add_row(*row)
        console.print(st)
        if hidden_count > 0:
            console.print(
                f"  [dim]（已隱藏 {hidden_count} 個雜訊符號：未中獎且格子機率 < "
                f"{_SYMBOL_DISPLAY_THRESHOLD:.1%}）[/dim]"
            )
    else:
        console.print()
        console.print(
            "  [yellow]⚠ 符號統計資料為空[/yellow]\n"
            "    1. 中獎次數可能還不夠，再多跑幾把就會累積；或\n"
            "    2. 早期版本的解析 bug — 請按 [bold]C → I[/bold] 重置分析後重新累積。"
        )

    # 線路統計
    li = stats.get("line_info", {})
    if li:
        console.print()
        console.rule("[bold]📐 線路統計[/]")
        lt = Table(show_header=True, header_style="bold cyan")
        lt.add_column("線路",     justify="left",  no_wrap=True)
        lt.add_column("命中次數", justify="right")
        lt.add_column("命中率",   justify="right")
        lt.add_column("總賠付",   justify="right")
        for ln, info in sorted(li.items(), key=lambda x: -x[1]["hits"]):
            lt.add_row(ln, f"{info['hits']:,}",
                       f"{info['hit_rate']:.1%}",
                       f"{info['total_payout']:,}")
        console.print(lt)

    # 時段分析（依 hour-of-day 從 history 算）
    history = state.get("history") or []
    if history:
        hourly = compute_hourly_breakdown(history)
        active = [h for h in hourly if h["bets"] > 0]
        if active:
            console.print()
            console.rule("[bold]🕐 時段分析（每小時）[/]")
            ht = Table(show_header=True, header_style="bold cyan")
            ht.add_column("時段", justify="right")
            ht.add_column("下注次數", justify="right")
            ht.add_column("勝率", justify="right")
            ht.add_column("平均賠率", justify="right")
            ht.add_column("總淨收", justify="right")
            ht.add_column("平均淨收", justify="right")

            # 找最賺 / 最虧 的時段，標色
            best_change = max((h["total_change"] for h in active), default=0)
            worst_change = min((h["total_change"] for h in active), default=0)

            for h in sorted(active, key=lambda r: r["hour"]):
                tc = h["total_change"]
                tc_color = "green" if tc > 0 else ("red" if tc < 0 else "dim")
                # 標出冠軍
                hr_str = f"{h['hour']:02d}:00"
                if h["bets"] >= 10 and tc == best_change and tc > 0:
                    hr_str = f"[bold green]🏆 {hr_str}[/bold green]"
                elif h["bets"] >= 10 and tc == worst_change and tc < 0:
                    hr_str = f"[bold red]💀 {hr_str}[/bold red]"

                ht.add_row(
                    hr_str,
                    f"{h['bets']:,}",
                    f"{h['win_rate']:.1%}",
                    f"{h['avg_multiplier']:.3f}x",
                    f"[{tc_color}]{tc:+,}[/{tc_color}]",
                    f"{h['avg_change']:+,.0f}",
                )
            console.print(ht)
            console.print(
                "  [dim]🏆/💀 標出 ≥10 把的時段裡最賺 / 最虧的（冷門時段不算）[/dim]"
            )

    # 紀錄摘要
    _show_history_summary(state)

    input("\n  按 Enter 返回...")


# ── Email 通知 ───────────────────────────────────────────────────────────────
def _send_email_sync(email_cfg: dict, subject: str, body: str) -> bool:
    log = logging.getLogger(__name__)
    if not email_cfg.get("enabled"):
        return False
    user = email_cfg.get("user") or ""
    pwd  = email_cfg.get("password") or ""
    to   = email_cfg.get("to") or ""
    if not (user and pwd and to):
        log.warning("Email 設定不完整（user/password/to 缺一），略過寄送")
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"]    = user
        msg["To"]      = to
        host = email_cfg.get("smtp_host", "smtp.gmail.com")
        port = int(email_cfg.get("smtp_port", 587))
        with smtplib.SMTP(host, port, timeout=15) as server:
            server.ehlo()
            server.starttls()
            server.login(user, pwd)
            server.send_message(msg)
        log.info("Email 已寄出: %s", subject)
        return True
    except Exception as e:
        log.warning("Email 寄送失敗: %s", e)
        return False


async def send_email(email_cfg: dict, subject: str, body: str) -> bool:
    """非同步寄信（在 executor 裡跑 blocking smtplib）。"""
    return await asyncio.get_event_loop().run_in_executor(
        None, _send_email_sync, email_cfg, subject, body
    )


# ── 貓娘派遣狀態解析 ─────────────────────────────────────────────────────────
def parse_dispatch_status(text: str) -> tuple[str, int | None]:
    """
    從 /check 回應文字解析貓娘派遣狀態。
    回傳 (status, remaining_minutes)：
      - "dispatching", N  →  派遣中，剩 N 分鐘
      - "not_dispatching", None  →  已完成或未派遣（找到 貓娘派遣 但無「派遣中」）
      - "unknown", None  →  完全沒找到
    """
    m = re.search(r'貓娘派遣[\s\S]{0,80}?派遣中\s*(\d+)\s*(小時|分鐘)', text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        return "dispatching", (n * 60 if unit == "小時" else n)
    if "貓娘派遣" in text:
        return "not_dispatching", None
    return "unknown", None


# ── 設定 ──────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


# （load/save_slot_analysis、load/save_history 搬到 slot_analysis 模組；
#  本檔頂端已 from slot_analysis import）


def save_config(config: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def ensure_gambling_defaults(config: dict, balance: int | None = None):
    """補齊 gambling / email / nekomusume 欄位預設值（只填未設定的欄位）。"""
    config.setdefault("log_level", "INFO")    # DEBUG / INFO / WARNING / ERROR
    gcfg = config.setdefault("gambling", {})
    gcfg.setdefault("enabled",         True)
    gcfg.setdefault("threshold",       5000)
    gcfg.setdefault("min_bet",         100)
    gcfg.setdefault("strategy",        "auto")
    gcfg.setdefault("bet_fraction",    0.02)
    gcfg.setdefault("interval_min",    DEFAULT_INTERVAL_MIN)
    gcfg.setdefault("interval_max",    DEFAULT_INTERVAL_MAX)
    gcfg.setdefault("goal",            DEFAULT_GOAL)
    gcfg.setdefault("notify_user_id",  DEFAULT_NOTIFY_USER_ID)
    gcfg.setdefault("goal_action",     DEFAULT_GOAL_ACTION)
    gcfg.setdefault("goal_step",       DEFAULT_GOAL_STEP)
    gcfg.setdefault("loss_floor",      DEFAULT_LOSS_FLOOR)
    gcfg.setdefault("loss_action",     DEFAULT_LOSS_ACTION)
    gcfg.setdefault("loss_step",       DEFAULT_LOSS_STEP)
    # 連敗冷靜：連敗 N 場後自動暫停 M 分鐘；0 = 不啟用
    gcfg.setdefault("loss_streak_pause",        0)   # N 場連敗觸發
    gcfg.setdefault("loss_streak_cooldown_min", 5)   # 觸發後冷靜 M 分鐘
    gcfg.setdefault("bigwin_multiplier", DEFAULT_BIGWIN_MULTIPLIER)
    if "max_bet" not in gcfg:
        excess = max(0, (balance or 0) - gcfg["threshold"])
        gcfg["max_bet"] = max(500, int(excess * 0.10))

    ecfg = config.setdefault("email", {})
    ecfg.setdefault("enabled",   False)
    ecfg.setdefault("smtp_host", "smtp.gmail.com")
    ecfg.setdefault("smtp_port", 587)
    ecfg.setdefault("user",      "")
    ecfg.setdefault("password",  "")
    ecfg.setdefault("to",        "")
    ecfg.setdefault("notify_goal",   True)   # 達標 email
    ecfg.setdefault("notify_loss",   True)   # 停損觸發 email
    ecfg.setdefault("notify_bigwin", True)   # 中大獎 email
    ecfg.setdefault("notify_dead",   True)   # bot 停擺 email
    ecfg.setdefault("notify_neko",   True)   # 貓娘完成 email
    ecfg.setdefault("notify_digest", True)   # 每日摘要 email
    ecfg.setdefault("digest_hour",   DEFAULT_DIGEST_HOUR)  # 0~23
    ecfg.setdefault("dead_threshold", DEFAULT_DEAD_THRESHOLD)

    ncfg = config.setdefault("nekomusume", {})
    ncfg.setdefault("enabled",          True)
    ncfg.setdefault("check_interval_min", DEFAULT_NEKOMUSUME_INTERVAL_MIN)
    ncfg.setdefault("auto_claim",        False)   # 偵測完成後自動 /nekomusume status + 點「領取並再派遣」

    tcfg = config.setdefault("transfer", {})
    tcfg.setdefault("enabled",      False)
    tcfg.setdefault("target",       "")        # 對象 — 用於 user picker 搜尋（顯示名稱或 user ID）
    tcfg.setdefault("amount",       100)       # 每次轉帳金額
    tcfg.setdefault("interval_min", DEFAULT_TRANSFER_INTERVAL_MIN)  # 多久一次（分鐘）

    dcfg = config.setdefault("dashboard", {})
    dcfg.setdefault("enabled", True)
    dcfg.setdefault("host",    "0.0.0.0")      # 預設讓同 LAN 手機可看；要鎖本機就改 "127.0.0.1"
    dcfg.setdefault("port",    8765)
    dcfg.setdefault("username", "admin")       # HTTP Basic Auth 用；password 空 = 不啟用驗證
    dcfg.setdefault("password", "")            # 空字串 = 不啟用密碼保護

    save_config(config)


# ── 押注策略 ──────────────────────────────────────────────────────────────────
def calculate_bet(balance: int, gcfg: dict,
                  slot_analysis: dict | None = None) -> int:
    threshold = gcfg.get("threshold", 5000)
    min_bet   = gcfg.get("min_bet",   100)
    max_bet   = gcfg.get("max_bet",   500)
    fraction  = gcfg.get("bet_fraction", 0.02)
    strategy  = gcfg.get("strategy",  "auto")

    excess = balance - threshold
    # 如果可動用的金額不足以下最小注（bot 拒絕 < min_bet 的下注），
    # 直接放棄這次下注 — 不要硬切到 excess 否則會被 bot 退回
    if excess < min_bet:
        return 0

    if strategy == "kelly" and slot_analysis is not None:
        stats = compute_slot_stats(slot_analysis)
        if stats.get("sufficient_data") and stats["kelly_fraction"] > 0:
            bet = max(min_bet, int(excess * stats["kelly_fraction"] / 2))
        else:
            bet = min_bet
    elif strategy == "fixed":
        bet = min_bet
    else:
        bet = max(min_bet, int(excess * fraction))

    if max_bet > 0:
        bet = min(bet, max_bet)
    # 確保最後 bet 仍 >= min_bet（max_bet 設太低時也不能低於 min_bet）
    bet = max(bet, min_bet)
    # 一定要 <= excess 才下注；否則放棄這把
    if bet > excess:
        return 0
    return bet


# ── UI 渲染 ───────────────────────────────────────────────────────────────────
def fmt_remaining(ts: float | None) -> str:
    if ts is None:
        return "─"
    remaining = ts - time.time()
    if remaining <= 0:
        return "[green]即將執行[/green]"
    h, r = divmod(int(remaining), 3600)
    m, s = divmod(r, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    if m > 0:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# ── Dashboard URL helpers（給 W / K 鍵與 UI 顯示用）─────────────────────────
_lan_ip_cache: list[str] = []   # [ip] — 算過就不要每幀都重抓


def _detect_lan_ip() -> str:
    """嘗試找本機 LAN IPv4；連不上回 'localhost'。Cache 結果避免重複呼叫。"""
    if _lan_ip_cache:
        return _lan_ip_cache[0]
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = "localhost"
    _lan_ip_cache.append(ip)
    return ip


def _dashboard_local_url(config: dict) -> str:
    dcfg = config.get("dashboard", {})
    port = int(dcfg.get("port", 8765))
    return f"http://127.0.0.1:{port}/"


def _dashboard_lan_url(config: dict) -> str:
    dcfg = config.get("dashboard", {})
    host = dcfg.get("host", "0.0.0.0")
    port = int(dcfg.get("port", 8765))
    if host == "0.0.0.0":
        ip = _detect_lan_ip()
    else:
        ip = host
    return f"http://{ip}:{port}/"


def _copy_to_clipboard(text: str) -> bool:
    """Windows 用內建 clip.exe；其他平台失敗回 False（使用者可看 log 手動複製）。"""
    try:
        import subprocess
        proc = subprocess.run(
            ["clip"], input=text, text=True, encoding="utf-8",
            timeout=2, capture_output=True,
        )
        return proc.returncode == 0
    except Exception:
        return False


def build_layout(state: dict, config: dict) -> Layout:
    gcfg        = config.get("gambling", {})
    balance     = state["balance"]
    start       = state["start_balance"]
    total_bets  = state["total_bets"]
    net         = state["net_change"]
    win_rate    = state["wins"] / total_bets * 100 if total_bets > 0 else 0

    if state.get("paused"):
        status_text  = "已暫停"
        status_color = "yellow"
    else:
        status_text  = state["status"]
        status_color = "green" if status_text == "運行中" else "yellow"

    # Header — 用 Text.from_markup 才會解析 [green] tags；直接 Text(...) 會當字面值
    header = Panel(
        Text.from_markup(
            f"🤖 Discord Auto Bot  |  [{status_color}]{status_text}[/{status_color}]",
            justify="center",
        ),
        style="bold cyan", height=3,
    )

    # 統計面板
    bal_str = f"[bold]{balance:,}[/bold]" if isinstance(balance, int) else "[dim]讀取中...[/dim]"
    if isinstance(balance, int) and isinstance(start, int):
        diff = balance - start
        dc = "green" if diff >= 0 else "red"
        diff_str = f"[{dc}]{'+' if diff > 0 else ''}{diff:,}[/{dc}]"
    else:
        diff_str = "─"
    nc = "green" if net >= 0 else "red"
    net_str = f"[{nc}]{'+' if net > 0 else ''}{net:,}[/{nc}]"
    start_str = f"{start:,}" if isinstance(start, int) else "─"
    cur_bet = state["current_bet"]

    # 目標進度
    goal = int(gcfg.get("goal", 0) or 0)
    if goal > 0 and isinstance(balance, int):
        pct = min(100.0, balance / goal * 100)
        gc = "green" if balance >= goal else "yellow"
        goal_str = f"[{gc}]{balance:,} / {goal:,} ({pct:.1f}%)[/{gc}]"
    elif goal > 0:
        goal_str = f"─ / {goal:,}"
    else:
        goal_str = "[dim]未設定[/dim]"

    # 停損狀態
    loss_floor = int(gcfg.get("loss_floor", 0) or 0)
    if loss_floor > 0 and isinstance(balance, int):
        if balance <= loss_floor:
            loss_str = f"[red]{balance:,} ≤ {loss_floor:,}（已觸發）[/red]"
        else:
            buffer = balance - loss_floor
            loss_str = f"[green]{balance:,} > {loss_floor:,}  (+{buffer:,} 緩衝)[/green]"
    elif loss_floor > 0:
        loss_str = f"─ > {loss_floor:,}"
    else:
        loss_str = "[dim]未設定[/dim]"

    t1 = Table(box=None, show_header=False, padding=(0, 2), expand=True)
    t1.add_column(style="dim", width=14)
    t1.add_column()
    t1.add_row("💰 目前餘額",  bal_str)
    t1.add_row("📌 起始餘額",  start_str)
    t1.add_row("📈 本次盈虧",  diff_str)
    t1.add_row("🏁 目標進度",  goal_str)
    t1.add_row("⛔ 停損狀態",  loss_str)
    t1.add_row("", "")
    t1.add_row("🎲 總下注",    str(total_bets))
    t1.add_row("✅ 獲勝",      f"[green]{state['wins']}[/green]")
    t1.add_row("❌ 失敗",      f"[red]{state['losses']}[/red]")
    t1.add_row("📊 勝率",      f"{win_rate:.1f}%")
    # Streak：current >0 連勝；<0 連敗；=0 無紀錄
    cs = state.get("current_streak", 0)
    if cs > 0:
        streak_str = f"[green]🔥 {cs} 連勝[/green]   max: {state.get('max_win_streak', 0)} / {state.get('max_loss_streak', 0)}"
    elif cs < 0:
        streak_str = f"[red]💀 {abs(cs)} 連敗[/red]   max: {state.get('max_win_streak', 0)} / {state.get('max_loss_streak', 0)}"
    else:
        streak_str = f"[dim]─[/dim]   max: {state.get('max_win_streak', 0)} / {state.get('max_loss_streak', 0)}"
    t1.add_row("🔥 Streak",   streak_str)

    # Profit per hour（用 session_start_ts 算）
    sess_start = state.get("session_start_ts")
    pp_str = "─"
    if sess_start:
        hrs = max(1/60, (time.time() - sess_start) / 3600)
        pph = state.get("net_change", 0) / hrs
        pp_color = "green" if pph >= 0 else "red"
        pp_str = f"[{pp_color}]{pph:+,.0f}[/{pp_color}] / 小時  ({hrs:.1f}h)"
    t1.add_row("⏱ 時薪",       pp_str)

    t1.add_row("💵 賭博淨收",  net_str)

    sa = state.get("slot_analysis", {})
    sa_n = sa.get("total_spins", 0)
    if sa_n > 0:
        sa_stats = compute_slot_stats(sa)
        ev_c = "green" if sa_stats["edge"] >= 0 else "red"
        ev_str = (f"[{ev_c}]{sa_stats['ev']:.3f}x ({sa_stats['edge']:+.1%})[/{ev_c}]"
                  f"  n={sa_n}")
    else:
        ev_str = "[dim]資料不足[/dim]"
    t1.add_row("📈 EV/期望值", ev_str)

    t1.add_row("", "")
    t1.add_row("🎯 當前下注",
               f"[yellow]{cur_bet:,}[/yellow]" if cur_bet else "─")
    stats_panel = Panel(t1, title="[bold]📊 統計[/bold]", border_style="blue")

    # 設定面板
    strategy    = gcfg.get("strategy", "auto")
    frac_pct    = f"{gcfg.get('bet_fraction', 0.02) * 100:.1f}%"
    max_bet     = gcfg.get("max_bet", 0)
    max_bet_str = f"{max_bet:,}" if max_bet > 0 else "自動"
    strat_label = f"[cyan]{strategy}[/cyan]"
    if strategy == "auto":
        strat_label += f" ({frac_pct})"
    elif strategy == "kelly":
        if sa_n >= MIN_KELLY_SAMPLES:
            kf = sa_stats["kelly_fraction"] if sa_n > 0 else 0
            strat_label += f" (f*={kf:.3f})"
        else:
            strat_label += f" ({sa_n}/{MIN_KELLY_SAMPLES})"

    i_min = gcfg.get("interval_min", DEFAULT_INTERVAL_MIN)
    i_max = gcfg.get("interval_max", DEFAULT_INTERVAL_MAX)
    interval_str = f"{i_min}-{i_max}s"

    notify_uid = gcfg.get("notify_user_id", DEFAULT_NOTIFY_USER_ID)
    notify_str = f"…{str(notify_uid)[-6:]}" if notify_uid else "[dim]未設定[/dim]"

    t2 = Table(box=None, show_header=False, padding=(0, 2), expand=True)
    t2.add_column(style="dim", width=14)
    t2.add_column()
    t2.add_row("⚙️ 策略",    strat_label)
    t2.add_row("🏦 保底門檻", f"{gcfg.get('threshold', 5000):,}")
    t2.add_row("⬇️ 最小下注", f"{gcfg.get('min_bet', 100):,}")
    t2.add_row("⬆️ 最大下注", max_bet_str)
    t2.add_row("⏱️ 下注間距", interval_str)
    t2.add_row("🏁 目標餘額", f"{goal:,}" if goal > 0 else "[dim]未設定[/dim]")
    t2.add_row("📣 通知對象", notify_str)
    t2.add_row("🎮 賭博",     "[green]啟用[/green]" if gcfg.get("enabled") else "[red]停用[/red]")
    t2.add_row("", "")
    t2.add_row("⏰ /hourly",  fmt_remaining(state.get("hourly_next")))
    t2.add_row("📅 /daily",   fmt_remaining(state.get("daily_next")))

    neko_st = state.get("neko_status", "unknown")
    neko_deadline = state.get("neko_deadline_ts")
    if neko_st == "dispatching" and neko_deadline is not None:
        # fmt_remaining 會即時算 deadline - now，每次 UI refresh 都更新
        neko_str = f"[yellow]派遣中 {fmt_remaining(neko_deadline)}[/yellow]"
    elif neko_st == "dispatching":
        neko_str = "[yellow]派遣中[/yellow]"
    elif neko_st == "not_dispatching":
        neko_str = "[green]待領取/閒置[/green]"
    else:
        neko_str = "[dim]─[/dim]"
    t2.add_row("🐱 貓娘",     neko_str)

    tcfg = config.get("transfer", {})
    if tcfg.get("enabled"):
        try:
            tr_amt = int(tcfg.get("amount", 0) or 0)
        except (TypeError, ValueError):
            tr_amt = 0
        tr_target = tcfg.get("target") or "—"
        tr_int = tcfg.get("interval_min", DEFAULT_TRANSFER_INTERVAL_MIN)
        transfer_str = f"[green]{tr_target} {tr_amt:,}/次 ({tr_int}m)[/green]"
    else:
        transfer_str = "[dim]停用[/dim]"
    t2.add_row("💸 自動轉帳",  transfer_str)

    # Dashboard URL — 同 LAN 手機可以直接開
    dcfg = config.get("dashboard", {})
    if dcfg.get("enabled", True):
        lan_url = _dashboard_lan_url(config)
        # Rich Table 的 cell 太窄會被截掉；URL 不太會超過 30 chars
        dash_str = f"[cyan]{lan_url}[/cyan]"
    else:
        dash_str = "[dim]停用[/dim]"
    t2.add_row("🌐 Dashboard", dash_str)

    cfg_panel = Panel(t2, title="[bold]⚙️ 設定[/bold]  [dim]C:修改系統設定[/dim]",
                      border_style="green")

    # 日誌
    lines    = state["log_lines"][-10:]
    log_text = "\n".join(lines) if lines else "[dim]尚無日誌[/dim]"
    log_panel = Panel(log_text, title="[bold]📋 日誌[/bold]", border_style="dim", height=13)

    # Footer
    pause_label = (
        "[yellow]P 恢復系統[/yellow]"
        if state.get("paused")
        else "[bold]P[/bold] 暫停系統"
    )
    footer = Panel(
        f"[dim][bold]Q[/bold] 退出  [bold]C[/bold] 修改系統設定  "
        f"{pause_label}  "
        f"[bold]E[/bold] 匯出分析結果  "
        f"[bold]S[/bold] 分析賭博機率  "
        f"[bold]W[/bold] 開啟 Dashboard  "
        f"[bold]K[/bold] 複製 Dashboard URL  "
        f"[bold]F[/bold] 重啟程式[/dim]",
        style="dim", height=3,
    )

    layout = Layout()
    layout.split_column(
        Layout(header,    name="header", size=3),
        Layout(name="body"),
        Layout(log_panel, name="logs",   size=13),
        Layout(footer,    name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(stats_panel, name="stats"),
        Layout(cfg_panel,   name="cfg"),
    )
    return layout


# ── 鍵盤監聽（背景執行緒）────────────────────────────────────────────────────
def start_kb_listener(state: dict):
    def _listen():
        while not state.get("quit"):
            try:
                if msvcrt.kbhit():
                    key = msvcrt.getch()
                    try:
                        state["pending_key"] = key.decode("utf-8").lower()
                    except UnicodeDecodeError:
                        pass
            except Exception:
                pass
            time.sleep(0.05)
    threading.Thread(target=_listen, daemon=True).start()


# ── 設定選單 ──────────────────────────────────────────────────────────────────
async def ainput(prompt: str) -> str:
    return await asyncio.get_event_loop().run_in_executor(None, input, prompt)


def _file_size_str(path: str) -> str:
    """回傳 path 的人類可讀大小（KB / MB），不存在則回傳 '—'。"""
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
    """回傳 (size_str, file_count)；不存在則 ('—', 0)。"""
    if not os.path.isdir(path):
        return "—", 0
    total = 0
    count = 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total += os.path.getsize(fp)
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


def _delete_file_safe(path: str) -> bool:
    """安全刪除單一檔案；不存在或失敗回傳 False。"""
    if not os.path.exists(path):
        return False
    try:
        os.remove(path)
        return True
    except OSError as e:
        print(f"  ⚠ 刪除失敗 {path}: {e}")
        return False


def _delete_dir_contents(path: str) -> tuple[int, int]:
    """刪除目錄內所有檔案（不刪目錄本身）；回傳 (刪除數, 失敗數)。"""
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


async def _do_system_update(state: dict) -> bool:
    """
    執行 git pull 拉取最新程式碼。
    成功且有更新 → 設 reboot 旗標讓 run.bat 重新啟動，回傳 True。
    沒更新或失敗 → 回傳 False。

    需求：本目錄是 git repo，且 git 有 cached credentials（user 已能 push 就 OK）。
    """
    import subprocess
    print("\n  正在執行 git pull origin main ...")
    try:
        proc = subprocess.run(
            ["git", "pull", "--ff-only", "origin", "main"],
            capture_output=True, text=True, encoding="utf-8",
            timeout=60,
        )
    except FileNotFoundError:
        print("  ⚠ 找不到 git 指令，請確認已安裝 Git 並加入 PATH")
        return False
    except subprocess.TimeoutExpired:
        print("  ⚠ git pull 超過 60 秒未完成（網路或認證問題？）")
        return False
    except Exception as e:
        print(f"  ⚠ git pull 發生錯誤: {e}")
        return False

    out = (proc.stdout or "") + (proc.stderr or "")
    print("  --- git output ---")
    for line in out.strip().splitlines()[-15:]:    # 只印最後 15 行避免洗版
        print(f"  {line}")
    print("  ------------------")

    if proc.returncode != 0:
        print(f"  ⚠ git pull 失敗（exit code {proc.returncode}）")
        return False

    if "Already up to date" in out or "Already up-to-date" in out:
        print("  ✓ 已是最新版本")
        return False

    # 有更新 — 觸發重啟
    print("  ✓ 更新成功！3 秒後重啟程式...")
    await asyncio.sleep(3)
    state["reboot"] = True
    state["quit"]   = True
    return True


async def run_advanced_menu(state: dict):
    """進階設定子選單：檔案管理 + 系統更新。"""
    while True:
        os.system("cls")
        print(f"\n{'═'*48}")
        print("  🛠️  進階設定")
        print(f"{'═'*48}")

        debug_size = _file_size_str("slot_debug.log")
        history_size = _file_size_str(HISTORY_PATH)
        analysis_size = _file_size_str(ANALYSIS_PATH)
        bot_log_size = _file_size_str(LOG_FILE_PATH)
        exports_size, exports_count = _dir_size_str(EXPORT_DIR)
        sa_spins = state.get("slot_analysis", {}).get("total_spins", 0)

        print("  [檔案管理]")
        print(f"   [1] 刪除 slot_debug.log              ({debug_size})")
        print(f"   [2] 刪除 exports/ 內所有檔案         ({exports_count} 檔, {exports_size})")
        print(f"   [3] 刪除 gambling_history.json       ({history_size})")
        print(f"   [4] 重置 slot_analysis.json          ({analysis_size}, {sa_spins} 筆)")
        print(f"   [5] 一鍵清除以上全部")
        print()
        print("  [日誌]")
        print(f"   [7] 開啟 bot.log                     ({bot_log_size})")
        print(f"   [8] 清空 bot.log + 輪替檔")
        print()
        print("  [系統]")
        print(f"   [6] 系統更新（git pull + 重啟）")
        print()
        print("  [0] 返回上一頁")
        print()

        choice = (await ainput("  選擇: ")).strip()

        if choice == "0":
            break
        elif choice == "1":
            confirm = (await ainput("  確認刪除 slot_debug.log？(y/N): ")).strip().lower()
            if confirm == "y":
                if _delete_file_safe("slot_debug.log"):
                    print("  ✓ 已刪除")
                else:
                    print("  （檔案不存在）")
                await ainput("  按 Enter 繼續...")
        elif choice == "2":
            confirm = (await ainput(
                f"  確認刪除 exports/ 內 {exports_count} 個檔案？(y/N): "
            )).strip().lower()
            if confirm == "y":
                d, f = _delete_dir_contents(EXPORT_DIR)
                print(f"  ✓ 刪除 {d} 個檔案" + (f"，{f} 個失敗" if f else ""))
                await ainput("  按 Enter 繼續...")
        elif choice == "3":
            confirm = (await ainput("  確認刪除 gambling_history.json？(y/N): ")).strip().lower()
            if confirm == "y":
                if _delete_file_safe(HISTORY_PATH):
                    state["history"] = []
                    print("  ✓ 已刪除（state.history 也清空）")
                else:
                    print("  （檔案不存在）")
                await ainput("  按 Enter 繼續...")
        elif choice == "4":
            confirm = (await ainput(
                f"  確認重置 slot_analysis.json？({sa_spins} 筆紀錄會清除) (y/N): "
            )).strip().lower()
            if confirm == "y":
                state["slot_analysis"] = _make_slot_analysis()
                _delete_file_safe(ANALYSIS_PATH)
                print(f"  ✓ 已重置")
                await ainput("  按 Enter 繼續...")
        elif choice == "5":
            confirm = (await ainput(
                "  ⚠ 確認刪除以上所有檔案＋重置分析？(yes 全字輸入確認): "
            )).strip().lower()
            if confirm == "yes":
                _delete_file_safe("slot_debug.log")
                d, _ = _delete_dir_contents(EXPORT_DIR)
                _delete_file_safe(HISTORY_PATH)
                state["history"] = []
                state["slot_analysis"] = _make_slot_analysis()
                _delete_file_safe(ANALYSIS_PATH)
                print(f"  ✓ 已清除全部（exports 刪 {d} 檔、debug log、history、分析資料）")
                await ainput("  按 Enter 繼續...")
        elif choice == "6":
            confirm = (await ainput(
                "  將執行 git pull origin main 並可能重啟。確定？(y/N): "
            )).strip().lower()
            if confirm == "y":
                rebooted = await _do_system_update(state)
                if rebooted:
                    return    # 直接退出選單，main loop 會看到 quit=True 退出
                await ainput("  按 Enter 繼續...")
        elif choice == "7":
            if os.path.exists(LOG_FILE_PATH):
                try:
                    os.startfile(LOG_FILE_PATH)
                    print(f"  ✓ 已用預設應用程式開啟 {LOG_FILE_PATH}")
                except OSError as e:
                    print(f"  ⚠ 無法開啟: {e}")
            else:
                print(f"  （{LOG_FILE_PATH} 不存在）")
            await ainput("  按 Enter 繼續...")
        elif choice == "8":
            confirm = (await ainput(
                "  確認清空 bot.log 與所有輪替檔（bot.log.1 .2 .3）？(y/N): "
            )).strip().lower()
            if confirm == "y":
                # 關掉檔案 handler 後再刪，否則 Windows 會檔案鎖
                root = logging.getLogger()
                file_handlers = [h for h in root.handlers
                                 if isinstance(h, logging.handlers.RotatingFileHandler)]
                for h in file_handlers:
                    h.close()
                    root.removeHandler(h)
                cleared = 0
                for path in [LOG_FILE_PATH] + [
                    f"{LOG_FILE_PATH}.{i}" for i in range(1, LOG_FILE_BACKUP_COUNT + 1)
                ]:
                    if _delete_file_safe(path):
                        cleared += 1
                # 重建 file handler
                try:
                    fh = logging.handlers.RotatingFileHandler(
                        LOG_FILE_PATH,
                        maxBytes=LOG_FILE_MAX_BYTES,
                        backupCount=LOG_FILE_BACKUP_COUNT,
                        encoding="utf-8",
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
                await ainput("  按 Enter 繼續...")


async def run_config_menu(state: dict, config_holder: list):
    os.system("cls")
    config = config_holder[0]
    gcfg   = config.setdefault("gambling", {})

    ecfg = config.setdefault("email",       {})
    ncfg = config.setdefault("nekomusume",  {})
    tcfg = config.setdefault("transfer",    {})

    while True:
        i_min  = gcfg.get('interval_min', DEFAULT_INTERVAL_MIN)
        i_max  = gcfg.get('interval_max', DEFAULT_INTERVAL_MAX)
        goal   = gcfg.get('goal', DEFAULT_GOAL)
        uid    = gcfg.get('notify_user_id', DEFAULT_NOTIFY_USER_ID)
        action = gcfg.get('goal_action', DEFAULT_GOAL_ACTION)
        step   = gcfg.get('goal_step', DEFAULT_GOAL_STEP)
        loss_f  = int(gcfg.get('loss_floor', DEFAULT_LOSS_FLOOR) or 0)
        loss_a  = gcfg.get('loss_action', DEFAULT_LOSS_ACTION)
        loss_s  = int(gcfg.get('loss_step', DEFAULT_LOSS_STEP) or 0)
        em_on  = ecfg.get('enabled', False)
        em_to  = ecfg.get('to', '')
        nk_on  = ncfg.get('enabled', True)
        bw_on  = ecfg.get('notify_bigwin', True)
        bw_mul = float(gcfg.get('bigwin_multiplier', DEFAULT_BIGWIN_MULTIPLIER))
        dd_on  = ecfg.get('notify_dead', True)
        dd_thr = int(ecfg.get('dead_threshold', DEFAULT_DEAD_THRESHOLD))
        ls_on  = ecfg.get('notify_loss', True)
        dg_on  = ecfg.get('notify_digest', True)
        dg_hr  = int(ecfg.get('digest_hour', DEFAULT_DIGEST_HOUR))

        print(f"\n{'═'*48}")
        print("  ⚙️  Discord Bot — 設定修改")
        print(f"{'═'*48}")
        print("  [賭博]")
        print(f"   [1] 保底門檻:    {gcfg.get('threshold', 5000):,}")
        print(f"   [2] 最小下注:    {gcfg.get('min_bet', 100):,}")
        print(f"   [3] 最大下注:    {gcfg.get('max_bet', 500):,}  (0 = 自動)")
        print(f"   [4] 押注比例:    {gcfg.get('bet_fraction', 0.02)*100:.1f}%  (auto策略用)")
        print(f"   [5] 策略:        {gcfg.get('strategy', 'auto')}  (auto / fixed / kelly)")
        print(f"   [6] 賭博:        {'啟用' if gcfg.get('enabled') else '停用'}")
        print(f"   [7] 下注間距:    {i_min}-{i_max} 秒")
        print("  [目標]")
        print(f"   [8] 目標餘額:    {goal:,}  (0 = 不設目標)")
        print(f"   [9] 通知 UID:    {uid}")
        print(f"   [A] 達標行為:    {action}  (pause = 停用; raise = 提升門檻續跑)")
        print(f"   [B] raise 步進:  {step:,}  (達標後 新目標 = 舊目標 + 步進)")
        print("  [停損]")
        print(f"   [N] 停損點:      {loss_f:,}  (0 = 不設停損)")
        print(f"   [O] 停損行為:    {loss_a}  (pause = 停用; lower_threshold = 下移門檻續跑)")
        print(f"   [R] 階梯下移步進: {loss_s:,}  (lower_threshold 模式：新門檻 = 餘額 - 步進)")
        print("  [Email 通知]")
        print(f"   [C] Email:       {'啟用' if em_on else '停用'}  收件人={em_to or '(未設定)'}")
        print(f"   [D] SMTP 設定 (host / port / user / password)")
        print(f"   [G] 中大獎通知:  {'啟用' if bw_on else '停用'}  賠率門檻={bw_mul:.1f}x")
        print(f"   [H] 停擺通知:    {'啟用' if dd_on else '停用'}  連續失敗門檻={dd_thr}")
        print(f"   [P] 停損通知:    {'啟用' if ls_on else '停用'}")
        print(f"   [T] 每日摘要:    {'啟用' if dg_on else '停用'}  寄送時段={dg_hr:02d}:00-{dg_hr:02d}:59")
        print(f"   [U] 摘要時段:    {dg_hr:02d}:00  (0~23 整點)")
        print("  [貓娘監控]")
        print(f"   [E] 貓娘監控:    {'啟用' if nk_on else '停用'}  (派遣完成自動 @ 通知)")
        print(f"   [F] 檢查間距:    {ncfg.get('check_interval_min', DEFAULT_NEKOMUSUME_INTERVAL_MIN)} 分鐘")
        sa_spins = state.get("slot_analysis", {}).get("total_spins", 0)
        print("  [Slot 分析]")
        print(f"   [I] 重置分析:    {sa_spins} 筆紀錄")
        tr_on  = tcfg.get('enabled', False)
        tr_tg  = tcfg.get('target', '')
        tr_amt = tcfg.get('amount', 0)
        tr_int = tcfg.get('interval_min', DEFAULT_TRANSFER_INTERVAL_MIN)
        print("  [自動轉帳]")
        print(f"   [J] 自動轉帳:    {'啟用' if tr_on else '停用'}")
        print(f"   [K] 對象 (名稱/UID): {tr_tg or '(未設定)'}")
        print(f"   [L] 金額:        {tr_amt:,}")
        print(f"   [M] 間距:        {tr_int} 分鐘")
        print()
        print("  [X] 進階（檔案管理 / 系統更新）")
        print()
        print("  [0] 儲存並返回")
        print()

        choice = (await ainput("  選擇: ")).strip().upper()

        if choice == "0":
            break
        elif choice == "1":
            raw = (await ainput(f"  保底門檻 (目前 {gcfg.get('threshold',5000):,}): ")).strip()
            if raw.isdigit():
                gcfg["threshold"] = int(raw); print(f"  ✓ 門檻 → {int(raw):,}")
        elif choice == "2":
            raw = (await ainput(f"  最小下注 (目前 {gcfg.get('min_bet',100):,}): ")).strip()
            if raw.isdigit():
                gcfg["min_bet"] = int(raw); print(f"  ✓ 最小下注 → {int(raw):,}")
        elif choice == "3":
            raw = (await ainput("  最大下注 (0=自動): ")).strip()
            if raw.isdigit():
                gcfg["max_bet"] = int(raw)
                print(f"  ✓ 最大下注 → {int(raw):,}" if int(raw) else "  ✓ 最大下注 → 自動")
        elif choice == "4":
            raw = (await ainput("  押注比例 % (例: 2 = 2%): ")).strip()
            try:
                gcfg["bet_fraction"] = float(raw) / 100
                print(f"  ✓ 押注比例 → {float(raw):.1f}%")
            except ValueError:
                print("  無效輸入")
        elif choice == "5":
            raw = (await ainput("  策略 (auto / fixed / kelly): ")).strip().lower()
            if raw in ("auto", "fixed", "kelly"):
                gcfg["strategy"] = raw; print(f"  ✓ 策略 → {raw}")
        elif choice == "6":
            gcfg["enabled"] = not gcfg.get("enabled", True)
            print(f"  ✓ 賭博 → {'啟用' if gcfg['enabled'] else '停用'}")
        elif choice == "7":
            raw_min = (await ainput(f"  最小間距秒數 (目前 {i_min}): ")).strip()
            raw_max = (await ainput(f"  最大間距秒數 (目前 {i_max}): ")).strip()
            try:
                if raw_min: gcfg["interval_min"] = max(0.0, float(raw_min))
                if raw_max: gcfg["interval_max"] = max(0.0, float(raw_max))
                if gcfg["interval_max"] < gcfg["interval_min"]:
                    gcfg["interval_max"] = gcfg["interval_min"]
                print(f"  ✓ 間距 → {gcfg['interval_min']}-{gcfg['interval_max']} 秒")
            except ValueError:
                print("  無效輸入")
        elif choice == "8":
            raw = (await ainput("  目標餘額 (0=取消): ")).strip()
            if raw.isdigit():
                gcfg["goal"] = int(raw)
                state["goal_reached"] = False
                print(f"  ✓ 目標 → {int(raw):,}")
        elif choice == "9":
            raw = (await ainput("  Discord User ID (數字串): ")).strip()
            if raw.isdigit():
                gcfg["notify_user_id"] = raw
                print(f"  ✓ 通知對象 → {raw}")
        elif choice == "A":
            raw = (await ainput("  達標行為 (pause / raise): ")).strip().lower()
            if raw in ("pause", "raise"):
                gcfg["goal_action"] = raw
                print(f"  ✓ 達標行為 → {raw}")
        elif choice == "B":
            raw = (await ainput(f"  raise 步進 (目前 {step:,}): ")).strip()
            if raw.isdigit():
                gcfg["goal_step"] = int(raw)
                print(f"  ✓ 步進 → {int(raw):,}")
        elif choice == "N":
            raw = (await ainput("  停損點 (0=取消): ")).strip()
            if raw.isdigit():
                gcfg["loss_floor"] = int(raw)
                state["loss_triggered"] = False    # 重設停損狀態
                print(f"  ✓ 停損點 → {int(raw):,}")
        elif choice == "O":
            raw = (await ainput("  停損行為 (pause / lower_threshold): ")).strip().lower()
            if raw in ("pause", "lower_threshold"):
                gcfg["loss_action"] = raw
                print(f"  ✓ 停損行為 → {raw}")
        elif choice == "R":
            raw = (await ainput(f"  階梯下移步進 (目前 {loss_s:,}): ")).strip()
            if raw.isdigit():
                gcfg["loss_step"] = int(raw)
                print(f"  ✓ 步進 → {int(raw):,}")
        elif choice == "P":
            ecfg["notify_loss"] = not ecfg.get("notify_loss", True)
            print(f"  ✓ 停損通知 → {'啟用' if ecfg['notify_loss'] else '停用'}")
        elif choice == "T":
            ecfg["notify_digest"] = not ecfg.get("notify_digest", True)
            print(f"  ✓ 每日摘要 → {'啟用' if ecfg['notify_digest'] else '停用'}")
        elif choice == "U":
            raw = (await ainput(f"  摘要時段 (0~23，目前 {dg_hr}): ")).strip()
            if raw.isdigit() and 0 <= int(raw) <= 23:
                ecfg["digest_hour"] = int(raw)
                print(f"  ✓ 摘要時段 → {int(raw):02d}:00")
            else:
                print("  無效輸入（必須是 0~23）")
        elif choice == "C":
            ecfg["enabled"] = not ecfg.get("enabled", False)
            print(f"  ✓ Email → {'啟用' if ecfg['enabled'] else '停用'}")
            if ecfg["enabled"]:
                raw = (await ainput(f"  收件人 (目前 {em_to or '(未設定)'}): ")).strip()
                if raw:
                    ecfg["to"] = raw
                    print(f"  ✓ 收件人 → {raw}")
        elif choice == "D":
            print(f"   目前 host={ecfg.get('smtp_host','smtp.gmail.com')} "
                  f"port={ecfg.get('smtp_port',587)} user={ecfg.get('user','')}")
            host = (await ainput("  SMTP host (Enter 跳過): ")).strip()
            port = (await ainput("  SMTP port (Enter 跳過): ")).strip()
            user = (await ainput("  SMTP user (Enter 跳過): ")).strip()
            pwd  = (await ainput("  SMTP password (Enter 跳過; Gmail 用 App Password): ")).strip()
            if host: ecfg["smtp_host"] = host
            if port.isdigit(): ecfg["smtp_port"] = int(port)
            if user: ecfg["user"] = user
            if pwd:  ecfg["password"] = pwd
            print("  ✓ SMTP 已更新")
        elif choice == "E":
            ncfg["enabled"] = not ncfg.get("enabled", True)
            print(f"  ✓ 貓娘監控 → {'啟用' if ncfg['enabled'] else '停用'}")
        elif choice == "F":
            raw = (await ainput("  檢查間距分鐘 (建議 15-60): ")).strip()
            try:
                ncfg["check_interval_min"] = max(1.0, float(raw))
                print(f"  ✓ 檢查間距 → {ncfg['check_interval_min']} 分鐘")
            except ValueError:
                print("  無效輸入")
        elif choice == "G":
            ecfg["notify_bigwin"] = not ecfg.get("notify_bigwin", True)
            print(f"  ✓ 中大獎通知 → {'啟用' if ecfg['notify_bigwin'] else '停用'}")
            if ecfg["notify_bigwin"]:
                raw = (await ainput(
                    f"  賠率門檻 (例: 5 = 5x；目前 {bw_mul:.1f}x): "
                )).strip()
                try:
                    if raw:
                        gcfg["bigwin_multiplier"] = max(1.0, float(raw))
                        print(f"  ✓ 賠率門檻 → {gcfg['bigwin_multiplier']:.1f}x")
                except ValueError:
                    print("  無效輸入")
        elif choice == "H":
            ecfg["notify_dead"] = not ecfg.get("notify_dead", True)
            print(f"  ✓ 停擺通知 → {'啟用' if ecfg['notify_dead'] else '停用'}")
            if ecfg["notify_dead"]:
                raw = (await ainput(
                    f"  連續失敗幾次算停擺 (目前 {dd_thr}): "
                )).strip()
                if raw.isdigit() and int(raw) >= 1:
                    ecfg["dead_threshold"] = int(raw)
                    print(f"  ✓ 連續失敗門檻 → {ecfg['dead_threshold']}")
        elif choice == "I":
            confirm = (await ainput("  確認重置所有 slot 分析資料？(y/N): ")).strip().lower()
            if confirm == "y":
                state["slot_analysis"] = _make_slot_analysis()
                if os.path.exists(ANALYSIS_PATH):
                    os.remove(ANALYSIS_PATH)
                print(f"  ✓ 分析資料已重置（{sa_spins} 筆清除）")
        elif choice == "J":
            tcfg["enabled"] = not tcfg.get("enabled", False)
            print(f"  ✓ 自動轉帳 → {'啟用' if tcfg['enabled'] else '停用'}")
        elif choice == "K":
            print("  注意：對象用於觸發 Discord user picker 的搜尋字串。")
            print("        可填顯示名稱片段或 user ID（純數字）。")
            raw = (await ainput(f"  對象 (目前 {tr_tg or '(未設定)'}): ")).strip()
            if raw:
                tcfg["target"] = raw
                print(f"  ✓ 對象 → {raw}")
        elif choice == "L":
            raw = (await ainput(f"  金額 (目前 {tr_amt:,}): ")).strip()
            if raw.isdigit() and int(raw) > 0:
                tcfg["amount"] = int(raw)
                print(f"  ✓ 金額 → {int(raw):,}")
            else:
                print("  無效輸入（需要正整數）")
        elif choice == "M":
            raw = (await ainput(f"  間距分鐘數 (目前 {tr_int}): ")).strip()
            try:
                v = float(raw)
                if v >= 1:
                    tcfg["interval_min"] = v
                    print(f"  ✓ 間距 → {v} 分鐘")
                else:
                    print("  無效輸入（最少 1 分鐘）")
            except ValueError:
                print("  無效輸入")
        elif choice == "X":
            await run_advanced_menu(state)
            if state.get("quit"):
                # 系統更新觸發了重啟 — 立刻退出設定選單
                break

    config["gambling"]   = gcfg
    config["email"]      = ecfg
    config["nekomusume"] = ncfg
    config["transfer"]   = tcfg
    save_config(config)
    config_holder[0] = config
    _log(state, "設定已更新並儲存")
    os.system("cls")


# ── 頁面操作 ──────────────────────────────────────────────────────────────────
async def human_type(page: Page, text: str):
    for char in text:
        await page.keyboard.type(char)
        await asyncio.sleep(random.uniform(TYPING_DELAY_MIN_MS, TYPING_DELAY_MAX_MS) / 1000)


# 比對 bot 回應裡的餘額／油幣數字（半形+全形冒號、空白、底線、頓號都吃）
BALANCE_PATTERNS = [
    r'餘額[\s:：|]*([0-9,，]+)',
    r'油幣[\s:：]*([0-9,，]+)',
]


# _parse_balance_int 搬到 slot_parser；本檔頂端已 import 進來


def _count_balance_mentions(text: str) -> int:
    """整頁 body.textContent 裡符合餘額/油幣 pattern 的次數。"""
    n = 0
    for pat in BALANCE_PATTERNS:
        n += len(re.findall(pat, text))
    return n


def _last_balance_value(text: str) -> int | None:
    """回傳整頁文字中位置最靠後的餘額/油幣數字。"""
    last_val = None
    last_pos = -1
    for pat in BALANCE_PATTERNS:
        for m in re.finditer(pat, text):
            if m.start() > last_pos:
                v = _parse_balance_int(m.group(1))
                if v is not None:
                    last_val = v
                    last_pos = m.start()
    return last_val


# 追蹤「新回應出現」的尾端視窗大小。
# 用整頁 count diff 在長時間運行後會失效——Discord 會把舊訊息從 DOM 最前端移除
# (virtualization)，新訊息加在尾端，導致全頁 count 「進一個出一個」沒變。
# 改成只比對最後 N 字元，舊訊息離開不影響、新訊息一定在這個視窗裡。
_REPLY_WINDOW_CHARS = 10000


def _new_reply_detected(before_text: str, current_text: str) -> bool:
    """
    判斷自 before_text 之後是否有新的「含餘額/油幣」回應出現。
    任一訊號成立即算 True：
      1. 整頁最後一個餘額數字變了（值不同 → 一定有新內容）
      2. 在最後 _REPLY_WINDOW_CHARS 字元視窗裡，餘額/油幣出現次數增加
    """
    bv = _last_balance_value(before_text)
    cv = _last_balance_value(current_text)
    if cv is not None and cv != bv:
        return True
    bt = before_text[-_REPLY_WINDOW_CHARS:] if len(before_text) > _REPLY_WINDOW_CHARS else before_text
    ct = current_text[-_REPLY_WINDOW_CHARS:] if len(current_text) > _REPLY_WINDOW_CHARS else current_text
    return _count_balance_mentions(ct) > _count_balance_mentions(bt)


# （slot embed 解析的 regex / 函式都搬到 slot_parser 模組；本檔頂端已 import）


# ── Slot 分析累加器 ────────────────────────────────────────────────────────────
# （_update_slot_analysis、compute_slot_stats、_parse_slot_change、_parse_balance_int
#  搬到 slot_analysis / slot_parser 模組；本檔頂端已 import）


async def read_initial_balance_from_history(page: Page) -> int | None:
    """從已載入的聊天記錄找最近的餘額；找不到回傳 None。"""
    text: str = await page.evaluate("() => document.body.textContent")
    return _last_balance_value(text)


async def _send_slash_command(page: Page, command: str, param: str = ""):
    """實際送指令的內部函式，呼叫端必須先持有 command_lock。"""
    log = logging.getLogger(__name__)
    log.info("準備送出指令: %s %s", command, param)
    input_box = page.locator('[data-slate-editor="true"]')
    await input_box.click()
    await asyncio.sleep(random.uniform(0.3, 0.8))
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)
    await human_type(page, command)
    await asyncio.sleep(1.5)
    await page.keyboard.press("Tab")
    await asyncio.sleep(random.uniform(0.4, 0.8))
    if param:
        await human_type(page, param)
        await asyncio.sleep(random.uniform(0.2, 0.5))
    await page.keyboard.press("Enter")
    await asyncio.sleep(random.uniform(0.5, 1.0))
    log.info("指令 %s %s 已送出", command, param)


async def send_slash_command(page: Page, command: str, param: str = ""):
    async with command_lock:
        await _send_slash_command(page, command, param)


async def _send_message(page: Page, text: str):
    """送純文字訊息（非 slash 指令）。呼叫端必須先持有 command_lock。
    用 keyboard.insert_text 一次插入，避免 `@` 觸發 mention autocomplete。"""
    log = logging.getLogger(__name__)
    log.info("送出訊息: %s", text)
    input_box = page.locator('[data-slate-editor="true"]')
    await input_box.click()
    await asyncio.sleep(random.uniform(0.3, 0.6))
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)
    await page.keyboard.insert_text(text)
    await asyncio.sleep(0.5)
    # 若 autocomplete 還是冒出來，按 Esc 關掉再送
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.2)
    # Esc 可能也清掉輸入框內容；保險起見再 insert 一次後立刻 Enter
    current_text = await page.evaluate(
        "() => document.querySelector('[data-slate-editor=\"true\"]')?.textContent || ''"
    )
    if text.strip() not in current_text:
        await page.keyboard.insert_text(text)
        await asyncio.sleep(0.3)
    await page.keyboard.press("Enter")
    await asyncio.sleep(0.5)


async def send_message(page: Page, text: str):
    async with command_lock:
        await _send_message(page, text)


async def notify_goal_reached(page: Page, balance: int, goal: int, user_id: str):
    """達標時 @ 使用者。使用 <@USER_ID> 格式，Discord 會解析成 mention。"""
    text = f"<@{user_id}> 已達成賭博目標！目前餘額 {balance:,} / 目標 {goal:,}"
    await send_message(page, text)


async def _send_transfer_command(page: Page, target: str, amount: int):
    """
    送 /transfer user:<target> amount:<amount>。

    target 用於觸發 user picker 的搜尋字串（顯示名稱片段或 user ID）。
    送出順序：/transfer → Tab（選 command 焦點到 user 參數）→ 輸入 target →
    Enter（從 autocomplete 選最上面那位）→ 輸入 amount → Enter。

    呼叫端必須已持有 command_lock。
    """
    log = logging.getLogger(__name__)
    input_box = page.locator('[data-slate-editor="true"]')
    await input_box.click()
    await asyncio.sleep(random.uniform(0.3, 0.8))
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)

    await human_type(page, "/transfer")
    await asyncio.sleep(1.5)
    await page.keyboard.press("Tab")          # 選 /transfer，焦點落在 user 參數
    await asyncio.sleep(random.uniform(0.5, 0.9))

    # 輸入 target；觸發 user picker
    await human_type(page, target)
    await asyncio.sleep(1.2)
    # 用 Enter 選擇 picker 最上面那位（Discord 預設高亮第一筆）
    await page.keyboard.press("Enter")
    await asyncio.sleep(random.uniform(0.4, 0.8))

    # 輸入金額（自動進入下一個必填參數）
    await human_type(page, str(amount))
    await asyncio.sleep(random.uniform(0.3, 0.6))
    await page.keyboard.press("Enter")        # 送出指令
    await asyncio.sleep(1.0)
    log.info("/transfer target=%s amount=%d 已送出", target, amount)


async def _click_confirm_transfer(page: Page, timeout: float = 15.0) -> bool:
    """
    等待並點擊「確認轉錢」按鈕。Discord button 是 <button> 元素帶文字。
    成功點擊回傳 True；超時回傳 False。
    """
    log = logging.getLogger(__name__)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            btn = page.locator('button:has-text("確認轉錢")').last
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=3000)
                log.info("已點擊「確認轉錢」按鈕")
                return True
        except Exception as e:
            log.debug("等待確認按鈕中: %s", e)
        await asyncio.sleep(0.5)
    log.warning("等待「確認轉錢」按鈕超時 (%.0fs)", timeout)
    return False


async def do_transfer(page: Page, target: str, amount: int) -> bool:
    """完整轉帳流程：送指令 + 點確認按鈕。回傳是否成功點擊。"""
    async with command_lock:
        await _send_transfer_command(page, target, amount)
        # 等待確認嵌入訊息出現再點按鈕（按鈕在 confirm embed 上）
        return await _click_confirm_transfer(page, timeout=15.0)


async def _click_button_with_text(page: Page, text: str, timeout: float = 15.0) -> bool:
    """通用：等待並點擊含特定文字的按鈕。"""
    log = logging.getLogger(__name__)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            btn = page.locator(f'button:has-text("{text}")').last
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=3000)
                log.info("已點擊「%s」按鈕", text)
                return True
        except Exception as e:
            log.debug("等待「%s」按鈕中: %s", text, e)
        await asyncio.sleep(0.5)
    log.warning("等待「%s」按鈕超時 (%.0fs)", text, timeout)
    return False


async def auto_claim_and_redispatch_neko(page: Page) -> bool:
    """
    /nekomusume status → 等待 ephemeral embed → 點「領取並再派遣」按鈕。
    成功 → 領了戰利品 + 自動再派遣一次；不需要再寫 /nekomusume claim。
    呼叫端必須先取得 command_lock。
    """
    log = logging.getLogger(__name__)
    log.info("貓娘自動領取 + 再派遣 — 送 /nekomusume status")
    # /nekomusume 是有 sub-command 的指令 — 把整串「/nekomusume status」當 command 一起打，
    # autocomplete 會 highlight 對應 sub-command；Tab 確認 + Enter 送出
    await _send_slash_command(page, "/nekomusume status", param="")
    # /nekomusume status 是 ephemeral，要等一下 embed 渲染
    await asyncio.sleep(2.5)
    ok = await _click_button_with_text(page, "領取並再派遣", timeout=15.0)
    if not ok:
        log.warning("找不到「領取並再派遣」按鈕 — 可能還在派遣中、或 button 文字變了")
    return ok


async def send_and_capture_balance(
    page: Page, command: str, param: str = "", timeout: float = 30.0,
    stability_sec: float = 2.0,
) -> int | None:
    """
    送出指令並偵測新餘額。

    策略：在 command_lock 內，先記錄送指令前整頁「餘額/油幣」出現次數，
    送出後輪詢；只要次數增加就讀「目前最後一個」餘額數字，並要求該值持續
    `stability_sec` 秒不變才回傳。

    為什麼要等穩定？實測 slot bot 的 embed 是「兩階段」渲染：
      1. 先扣下注 → 顯示 餘額 = 舊餘額 - 下注（中間狀態，看起來像輸）
      2. 動畫跑完才 edit message → 顯示真正的 餘額 = 中間值 + 獎金
    只比對「連兩次相同」的話 1 秒內就會以中間值收工，因此贏的 slot
    永遠被誤判為輸。把穩定門檻拉長到動畫之外即可避免。

    這個做法不依賴 body.textContent 長度差或 chat-messages- count，可同時處理：
      - /balance 的 ephemeral 回應（不在 chat-messages- 裡）
      - /slot 的 embed 回應（含 button 文字干擾與兩階段更新）
      - Discord 在輸入過程中插入又移除的 autocomplete 節點
    """
    log = logging.getLogger(__name__)
    async with command_lock:
        before_text = await page.evaluate("() => document.body.textContent")

        await _send_slash_command(page, command, param)

        deadline = time.time() + timeout
        last_val: int | None = None
        last_change_time: float = time.time()
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            current_text = await page.evaluate("() => document.body.textContent")
            if not _new_reply_detected(before_text, current_text):
                continue
            val = _last_balance_value(current_text)
            if val is None:
                continue
            if val != last_val:
                last_val = val
                last_change_time = time.time()
                continue
            if time.time() - last_change_time >= stability_sec:
                return val

        if last_val is not None:
            log.info("%s %s timeout 但已抓到值 %d，採用之", command, param, last_val)
            return last_val
        log.warning("%s %s 在 %.0fs 內未取得新餘額", command, param, timeout)
        return None


async def get_balance(page: Page) -> int | None:
    # /balance 沒有動畫，1 秒穩定即可
    return await send_and_capture_balance(page, "/balance", timeout=30.0, stability_sec=1.0)


async def play_slot(page: Page, bet: int) -> dict | None:
    """
    送 /slot 並讀回新餘額 + 這局淨變動。

    回傳 {"balance": int, "change": int | None} 或 None（送指令完全失敗）。
    change 從 slot embed 的 "總計贏得 X" / "損失 X" 文字直接解析，比
    `new_balance - old_balance` 可靠：後者會被 hourly/daily 在中間夾帶的餘額
    變動污染，導致贏/輸判定錯誤。

    /slot 動畫通常 2-3 秒，等 5 秒穩定才採用，避免抓到「先扣下注、未加獎金」
    的中間狀態。
    """
    log = logging.getLogger(__name__)
    timeout = 45.0
    stability_sec = 5.0

    async with command_lock:
        # 用 alt-aware 取文字（含 <img> alt），確保 emoji 也能讀到
        before_text = await _get_page_text(page)

        await _send_slash_command(page, "/slot", param=str(bet))

        deadline = time.time() + timeout
        last_val: int | None = None
        last_change_time: float = time.time()
        last_text = before_text
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            current_text = await _get_page_text(page)
            last_text = current_text
            if not _new_reply_detected(before_text, current_text):
                continue
            val = _last_balance_value(current_text)
            if val is None:
                continue
            if val != last_val:
                last_val = val
                last_change_time = time.time()
                continue
            if time.time() - last_change_time >= stability_sec:
                change = _parse_slot_change(current_text, bet)
                lines = _parse_slot_lines(current_text)
                grid = _parse_slot_grid(current_text)
                # 這把 bet > 0 但 lines 或 grid 解析不到 → 寫 debug log 方便排查
                if change is not None and change > 0 and not lines:
                    _debug_dump_slot_text(current_text, "win but no lines parsed")
                if grid is None:
                    _debug_dump_slot_text(current_text, "grid not parsed")
                return {"balance": val, "change": change,
                        "lines": lines, "grid": grid}

        if last_val is not None:
            change = _parse_slot_change(last_text, bet)
            lines = _parse_slot_lines(last_text)
            grid = _parse_slot_grid(last_text)
            if change is not None and change > 0 and not lines:
                _debug_dump_slot_text(last_text, "win but no lines parsed (timeout path)")
            if grid is None:
                _debug_dump_slot_text(last_text, "grid not parsed (timeout path)")
            log.info("/slot %d timeout 但已抓到值 %d (change=%s)", bet, last_val, change)
            return {"balance": last_val, "change": change,
                    "lines": lines, "grid": grid}
        log.warning("/slot %d 在 %.0fs 內未取得新餘額", bet, timeout)
        return None


async def navigate_to_channel(page: Page, guild_id: str, channel_id: str):
    log = logging.getLogger(__name__)
    url = f"https://discord.com/channels/{guild_id}/{channel_id}"
    log.info("導航至頻道: %s", url)
    await page.goto(url, wait_until="domcontentloaded")
    try:
        await page.wait_for_selector('[data-slate-editor="true"]', timeout=30_000)
    except Exception as e:
        # 在這裡 timeout 通常是 storage_state 過期 → Discord 把我們導去 /login
        # 給比較清楚的錯誤訊息，不要讓使用者看那一堆 playwright traceback
        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""
        if "/login" in current_url or "/register" in current_url:
            msg = (
                "Discord session 已過期（storage_state.json 失效）。"
                "請執行 login.bat 重新登入後再啟動 run.bat。"
            )
        elif "discord.com/channels/" not in current_url:
            msg = (
                f"無法載入頻道頁，目前位置: {current_url}。"
                "可能是 storage_state 過期、guild_id/channel_id 設定錯誤、"
                "或網路連線問題。請檢查 config.json 的 ID 是否正確；"
                "若仍然不行，執行 login.bat 重新登入。"
            )
        else:
            msg = (
                f"頻道頁載入超過 30 秒仍找不到輸入框。網路太慢？Discord 改 UI？"
                f"目前位置: {current_url}"
            )
        log.error(msg)
        # 提到 stdout 也顯示一次（cmd 視窗會看到）
        print()
        print("=" * 70)
        print(f"[啟動失敗] {msg}")
        print("=" * 70)
        raise RuntimeError(msg) from e
    log.info("頻道已載入")


RECOVER_PAGE_FAILS_BEFORE_BROWSER_RESTART = 3   # recover_page 連續失敗 N 次就觸發整個 browser 重啟


async def recover_page(page: Page, state: dict) -> bool:
    """
    page state 變糟（連續多次 timeout）時重新載入頻道。
    回傳是否復原成功。整段在 command_lock 內，避免和其他 loop 撞。

    若 recover_page 自己連續失敗 N 次（page reload 都救不回來），
    觸發整個 browser 重啟（透過 reboot exit code 由 run.bat 重 launch）—
    比直接在 asyncio 裡換 browser 安全（避免 page reference 散落各處）。
    """
    log = logging.getLogger(__name__)
    guild_id = state.get("guild_id")
    channel_id = state.get("channel_id")
    if not guild_id or not channel_id:
        return False
    async with command_lock:
        log.warning("page 連續無回應，嘗試重新載入頻道...")
        try:
            await navigate_to_channel(page, guild_id, channel_id)
            await asyncio.sleep(3)
            log.info("頻道復原完成")
            state["recover_fail_streak"] = 0   # 成功 → reset
            return True
        except Exception as e:
            log.error("頻道復原失敗: %s", e)
            state["recover_fail_streak"] = state.get("recover_fail_streak", 0) + 1
            if state["recover_fail_streak"] >= RECOVER_PAGE_FAILS_BEFORE_BROWSER_RESTART:
                log.error(
                    "recover_page 連續 %d 次失敗 → 觸發整個 browser 重啟 (reboot)",
                    state["recover_fail_streak"],
                )
                _log(state, f"⚠ 連續 {state['recover_fail_streak']} 次 recover 失敗，請求重啟 browser")
                # 寄通知（如 email 啟用）
                ecfg = config_holder_ref[0].get("email", {}) if config_holder_ref else {}
                if ecfg.get("enabled") and ecfg.get("notify_dead", True):
                    try:
                        await send_email(
                            ecfg, "[Discord Bot] ⚠️ Browser 重啟觸發",
                            f"連續 {state['recover_fail_streak']} 次無法 recover_page，"
                            "已請求 reboot；run.bat 應該會自動重新啟動 bot。",
                        )
                    except Exception:
                        pass
                state["reboot"] = True
                state["quit"] = True
            return False


# 全域 config_holder reference — recover_page 會在沒拿到 config_holder 引數時拿來用
# 在 main() 開始時設成當前的 holder list；不是非常乾淨但避免改 recover_page 的 signature
config_holder_ref: list | None = None


# ── 排程迴圈 ──────────────────────────────────────────────────────────────────
async def _wait_while_paused(state: dict):
    """暫停期間原地停留；恢復或退出時返回。"""
    while state.get("paused") and not state["quit"]:
        await asyncio.sleep(0.5)


def _seconds_until_next_hour_boundary(now: datetime | None = None) -> float:
    """從現在到下個整點 (HH:00:00) 的秒數。"""
    now = now or datetime.now()
    next_hour = (now.replace(minute=0, second=0, microsecond=0)
                 + timedelta(hours=1))
    return max(0.0, (next_hour - now).total_seconds())


async def hourly_loop(page: Page, state: dict, config_holder: list):
    """
    /hourly 領取迴圈 — 錨定到時鐘整點。

    Discord bot 的 /hourly 在每個整點重置（reset 是時鐘 hour boundary，不是
    上次領取的 +60 min）。所以不能用 60min ± 8min jitter — 那會錯位。
    新策略：
      1. 等到下個整點 + [30s, 180s] 隨機，送 /hourly
      2. 之後每次都重新算「離下個整點還多久」，再睡到那個時間點
    這樣每個小時都剛好領一次，不會錯過、也不會超送。
    """
    log = logging.getLogger(__name__)

    while not state["quit"]:
        await _wait_while_paused(state)
        if state["quit"]:
            break

        # 等到下個整點 + 隨機 jitter
        wait_to_boundary = _seconds_until_next_hour_boundary()
        jitter = random.uniform(HOURLY_POST_BOUNDARY_MIN_SEC,
                                HOURLY_POST_BOUNDARY_MAX_SEC)
        delay = wait_to_boundary + jitter
        state["hourly_next"] = time.time() + delay
        log.info("下次 /hourly 在 %.0f 秒後（過下個整點 +%.0fs jitter）",
                 delay, jitter)
        await interruptible_sleep(state, delay)
        if state["quit"]:
            break
        await _wait_while_paused(state)
        if state["quit"]:
            break

        # 用 send_and_capture_balance 順便把回應裡的「當前餘額」抓下來更新 state，
        # 避免 gambling_loop 用過期 balance 計算下注，也避免 hourly 加的點數
        # 被誤算進 slot 勝負
        new_bal = await send_and_capture_balance(
            page, "/hourly", timeout=20.0, stability_sec=2.0
        )
        if new_bal is not None:
            state["balance"] = new_bal
            state["events"]["hourly_claims"] += 1
            log.info("/hourly 完成，餘額更新為 %d", new_bal)
            await _maybe_notify_goal(page, state, config_holder)
        else:
            # 偶發失敗：可能 bot 還沒 reset、或 race 沒抓到。短時間內重試一次
            log.info("/hourly 已送出但未取得新餘額，5 分鐘後再試一次")
            await interruptible_sleep(state, 300)
            if state["quit"]:
                break
            new_bal = await send_and_capture_balance(
                page, "/hourly", timeout=20.0, stability_sec=2.0
            )
            if new_bal is not None:
                state["balance"] = new_bal
                state["events"]["hourly_claims"] += 1
                log.info("/hourly 重試成功，餘額 %d", new_bal)
                await _maybe_notify_goal(page, state, config_holder)
            else:
                log.warning("/hourly 重試仍失敗，跳過此小時，等下個整點")


async def transfer_loop(page: Page, state: dict, config_holder: list):
    """
    自動轉帳迴圈：每 N 分鐘對指定對象 /transfer，並按下「確認轉錢」按鈕。
    config["transfer"] = {enabled, target, amount, interval_min}
    """
    log = logging.getLogger(__name__)
    while not state["quit"]:
        await _wait_while_paused(state)
        if state["quit"]:
            break

        tcfg = config_holder[0].get("transfer", {})
        if not tcfg.get("enabled", False):
            await interruptible_sleep(state, 30)
            continue

        target = (tcfg.get("target") or "").strip()
        try:
            amount = int(tcfg.get("amount", 0) or 0)
        except (TypeError, ValueError):
            amount = 0

        if not target or amount <= 0:
            log.warning("自動轉帳設定不完整 (target=%r, amount=%d)，30 秒後重試",
                        target, amount)
            await interruptible_sleep(state, 30)
            continue

        try:
            ok = await do_transfer(page, target, amount)
            if ok:
                state["events"]["transfers"] += 1
                _log(state, f"💸 已轉帳 {amount:,} → {target}")
            else:
                _log(state, f"⚠ 轉帳指令送出但找不到確認按鈕 ({target} {amount:,})")
        except Exception as e:
            log.error("自動轉帳失敗: %s", e)
            _log(state, f"⚠ 自動轉帳發生錯誤: {e}")

        try:
            interval = float(tcfg.get("interval_min", DEFAULT_TRANSFER_INTERVAL_MIN))
        except (TypeError, ValueError):
            interval = DEFAULT_TRANSFER_INTERVAL_MIN
        interval = max(1.0, interval)
        await interruptible_sleep(state, interval * 60)


async def daily_loop(page: Page, state: dict, config_holder: list):
    log = logging.getLogger(__name__)
    startup = random.uniform(0, DAILY_STARTUP_DELAY_SEC)
    state["daily_next"] = time.time() + startup
    log.info("/daily 將在 %.0f 秒後送出第一次", startup)
    await interruptible_sleep(state, startup)

    while not state["quit"]:
        await _wait_while_paused(state)
        if state["quit"]:
            break
        new_bal = await send_and_capture_balance(
            page, "/daily", timeout=20.0, stability_sec=2.0
        )
        if new_bal is not None:
            state["balance"] = new_bal
            state["events"]["daily_claims"] += 1
            log.info("/daily 完成，餘額更新為 %d", new_bal)
            await _maybe_notify_goal(page, state, config_holder)
        else:
            log.info("/daily 已送出（未取得新餘額，可能尚未到時間）")
        delay = DAILY_BASE_SEC + random.uniform(-DAILY_JITTER_SEC, DAILY_JITTER_SEC)
        state["daily_next"] = time.time() + delay
        await interruptible_sleep(state, delay)


async def _read_check_response(page: Page, timeout: float = 20.0) -> str | None:
    """
    送 /check（ephemeral）並等待新派遣資訊出現。回傳整頁文字（讓上層 parse）。

    舊版用 body.textContent length diff 偵測，但 ephemeral 的位置／長度
    在長時間運行後常常不可靠（Discord virtualization）；這版改成直接比對
    「整頁解析出來的派遣狀態」前後是否變化，不依賴 DOM 位置與長度。
    """
    log = logging.getLogger(__name__)
    async with command_lock:
        before_text = await page.evaluate("() => document.body.textContent")
        before_st, before_min = parse_dispatch_status(before_text)
        await _send_slash_command(page, "/check")
        # 給 ephemeral popup 至少 2 秒時間出現再開始判斷
        await asyncio.sleep(2.0)

        deadline = time.time() + timeout
        last_st, last_min, last_change = None, None, time.time()
        while time.time() < deadline:
            current = await page.evaluate("() => document.body.textContent")
            cur_st, cur_min = parse_dispatch_status(current)
            if (cur_st, cur_min) != (before_st, before_min):
                # 偵測到變化；要求穩定 1 秒避免讀到還在渲染的中間值
                if (cur_st, cur_min) == (last_st, last_min):
                    if time.time() - last_change >= 1.0:
                        return current
                else:
                    last_st, last_min = cur_st, cur_min
                    last_change = time.time()
            await asyncio.sleep(0.5)

        # 沒偵測到變化（狀態與之前相同 = 可能就是同一個派遣還沒換）
        # 回傳最新整頁讓上層 parse 一次，至少 UI 會看到目前狀態
        log.info("/check 未偵測到狀態變化，回傳當前整頁文字讓上層解析")
        return await page.evaluate("() => document.body.textContent")


async def nekomusume_loop(page: Page, state: dict, config_holder: list):
    """
    啟動 / reboot 後送一次 /check 對齊派遣剩餘時間，把 deadline 時間戳存到 state，
    之後純粹本地倒數（UI 也用這個 timestamp 算即時剩餘）。只有倒數結束時再
    送一次 /check 確認 → 通知 → 等待新派遣再對齊。

    每次派遣只會送 2 次 /check：開始一次對齊、結束一次確認，不再每 30 分鐘輪詢洗版。
    """
    log = logging.getLogger(__name__)
    last_status: str | None = None

    async def _query_status() -> tuple[str, int | None]:
        text = await _read_check_response(page)
        state["neko_last_check_ts"] = time.time()
        if text is None:
            return "unknown", None
        return parse_dispatch_status(text)

    async def _notify_completion():
        state["events"]["neko_completes"] += 1
        ncfg = config_holder[0].get("nekomusume", {})
        user_id = str(config_holder[0].get("gambling", {})
                      .get("notify_user_id", DEFAULT_NOTIFY_USER_ID))

        # 自動領取並再派遣（如啟用）
        auto_claimed = False
        if ncfg.get("auto_claim", False):
            try:
                async with command_lock:
                    auto_claimed = await auto_claim_and_redispatch_neko(page)
                if auto_claimed:
                    state["events"]["neko_completes"] += 0   # 已計過，不重複
                    log.info("貓娘已自動領取並再派遣")
                    _log(state, "🐱 貓娘已自動領取並再派遣")
            except Exception as e:
                log.warning("自動領取失敗: %s", e)
                _log(state, f"⚠ 貓娘自動領取失敗: {e}")

        # 通知訊息：成功自動領取 vs 需要手動
        try:
            if auto_claimed:
                msg = f"<@{user_id}> 貓娘派遣已完成 — 已自動領取並再派遣 🎉"
            else:
                msg = f"<@{user_id}> 貓娘派遣已完成！記得 `/nekomusume claim` 領取戰利品"
            await send_message(page, msg)
            log.info("已送出貓娘完成通知")
        except Exception as e:
            log.warning("貓娘完成通知失敗: %s", e)
        ecfg = config_holder[0].get("email", {})
        if ecfg.get("enabled") and ecfg.get("notify_neko", True):
            body = ("貓娘派遣已完成，已自動領取並再派遣 🎉" if auto_claimed
                    else "貓娘派遣已完成，請至 Discord 用 /nekomusume claim 領取。")
            await send_email(ecfg, "[Discord Bot] 貓娘派遣已完成", body)

    while not state["quit"]:
        await _wait_while_paused(state)
        if state["quit"]:
            break

        ncfg = config_holder[0].get("nekomusume", {})
        if not ncfg.get("enabled", True):
            state["neko_deadline_ts"] = None
            await interruptible_sleep(state, 300)
            continue

        deadline_ts = state.get("neko_deadline_ts")
        now = time.time()

        # ── 本地倒數中 → 不發任何指令，睡到 deadline ──
        if deadline_ts is not None and now < deadline_ts:
            await interruptible_sleep(state, deadline_ts - now)
            continue

        # ── 尚未對齊 / 倒數已到 → 送一次 /check ──
        new_status, minutes = await _query_status()
        state["neko_status"] = new_status

        if new_status == "dispatching" and minutes is not None:
            # 鎖定 deadline，之後純本地倒數
            state["neko_deadline_ts"] = now + minutes * 60
            log.info("貓娘派遣中：剩 %d 分鐘，鎖定本地倒數（不再輪詢）", minutes)
        elif new_status == "dispatching":
            # 解析不到時間 → 用基本間距重試
            state["neko_deadline_ts"] = None
            log.info("貓娘派遣中但解析不到時間，稍後重試")
        else:
            # 不在派遣中：上一輪是 dispatching → 派遣完成的轉折點，通知
            state["neko_deadline_ts"] = None
            if last_status == "dispatching":
                await _notify_completion()
            else:
                log.info("貓娘狀態: %s（閒置）", new_status)

        last_status = new_status

        # 不在派遣中 / 對齊失敗 → 用 base 間距再對齊（讓使用者派出新貓娘後能被偵測）
        if state.get("neko_deadline_ts") is None:
            base_min = float(ncfg.get("check_interval_min",
                                      DEFAULT_NEKOMUSUME_INTERVAL_MIN))
            await interruptible_sleep(state, base_min * 60)


async def _notify_bigwin(state: dict, config_holder: list,
                         bet: int, gross_win: int, multiplier: float):
    """
    /slot 中大獎時寄 email（Discord 那邊不另發訊息，避免洗版）。
    `gross_win` 是 slot embed 的「總計贏得」原始數字（含本金），
    `multiplier` = gross_win / bet。
    """
    log = logging.getLogger(__name__)
    log.info("🎰 中大獎！下注 %d → 贏得 %d (%.2fx)", bet, gross_win, multiplier)
    state["events"]["bigwins"] += 1

    ecfg = config_holder[0].get("email", {})
    if not (ecfg.get("enabled") and ecfg.get("notify_bigwin", True)):
        return

    bal = state.get("balance")
    bal_str = f"{bal:,}" if isinstance(bal, int) else "未知"
    body = (
        f"🎰 Discord Bot 中大獎通知\n\n"
        f"下注:        {bet:,}\n"
        f"總計贏得:    {gross_win:,}\n"
        f"淨變動:      +{gross_win - bet:,}\n"
        f"賠率:        {multiplier:.2f}x\n"
        f"目前餘額:    {bal_str}\n"
        f"累計勝/敗:   {state['wins']} / {state['losses']}\n"
        f"賭博淨收:    {state['net_change']:+,}"
    )
    await send_email(
        ecfg, f"[Discord Bot] 🎰 中大獎 {multiplier:.1f}x！贏得 {gross_win:,}", body
    )


async def _notify_dead(state: dict, config_holder: list, fail_count: int,
                        context: str = ""):
    """
    連續讀餘額失敗達門檻時，寄 email 提醒使用者 bot 可能掛了。
    用 state["dead_notified"] 確保同一個「死掉」期間只寄一次；
    任何一次成功讀餘額後 fail_count 歸零、dead_notified 也應重置。
    """
    log = logging.getLogger(__name__)
    if state.get("dead_notified"):
        return

    ecfg = config_holder[0].get("email", {})
    if not (ecfg.get("enabled") and ecfg.get("notify_dead", True)):
        return

    state["dead_notified"] = True
    bal = state.get("balance")
    bal_str = f"{bal:,}" if isinstance(bal, int) else "未知"
    body = (
        f"⚠️ Discord Bot 可能停擺\n\n"
        f"連續失敗次數: {fail_count}\n"
        f"上次成功餘額: {bal_str}\n"
        f"說明: /balance 與 /slot 連續無法解析回應，已嘗試 reload 頻道頁面。\n"
        f"建議檢查：Discord 登入是否過期、目標 bot 是否在線、頻道權限是否正常。\n\n"
        f"{context}".strip()
    )
    await send_email(
        ecfg, f"[Discord Bot] ⚠️ 警告：bot 可能停擺（連 {fail_count} 次失敗）", body
    )
    log.warning("已寄出 bot 停擺警告 email（連 %d 次失敗）", fail_count)


def _build_digest_body(state: dict) -> str:
    """組出每日摘要 email 內文，內容涵蓋區間統計、餘額、slot 分析。"""
    ev = state.get("events", {})
    since = ev.get("since_ts", state.get("session_start_ts", time.time()))
    now = time.time()
    hours = max(0.1, (now - since) / 3600)

    bal = state.get("balance")
    bal_str = f"{bal:,}" if isinstance(bal, int) else "未知"
    start = state.get("start_balance")
    if isinstance(start, int) and isinstance(bal, int):
        diff_str = f"{(bal - start):+,}"
    else:
        diff_str = "─"

    # 區間下注紀錄（只看 since 之後）
    history = state.get("history") or []
    period_records = [
        r for r in history
        if _record_ts_after(r, since)
    ]
    p_total = len(period_records)
    p_wins  = sum(1 for r in period_records if r.get("change", 0) > 0)
    p_loss  = sum(1 for r in period_records if r.get("change", 0) < 0)
    p_net   = sum(r.get("change", 0) for r in period_records)
    p_wr    = (p_wins / p_total * 100) if p_total else 0.0

    # Slot 分析（累計，不只本區間）
    sa = state.get("slot_analysis", {})
    sa_stats = compute_slot_stats(sa) if sa.get("total_spins") else None

    lines = [
        "📊 Discord Bot 每日摘要",
        f"區間: 過去 {hours:.1f} 小時",
        "",
        "━━━━━━ 餘額 ━━━━━━",
        f"目前餘額:   {bal_str}",
        f"起始餘額:   {start if start is not None else '未知'}",
        f"累計盈虧:   {diff_str}",
        "",
        "━━━━━━ 本區間下注 ━━━━━━",
        f"下注次數:   {p_total}",
        f"勝/敗:      {p_wins} / {p_loss}",
        f"勝率:       {p_wr:.1f}%",
        f"區間淨收:   {p_net:+,}",
        "",
        "━━━━━━ 本區間事件 ━━━━━━",
        f"/hourly 領取:    {ev.get('hourly_claims', 0)}",
        f"/daily 領取:     {ev.get('daily_claims', 0)}",
        f"自動轉帳:        {ev.get('transfers', 0)}",
        f"貓娘完成:        {ev.get('neko_completes', 0)}",
        f"中大獎次數:      {ev.get('bigwins', 0)}",
        f"達標次數:        {ev.get('goal_hits', 0)}",
        f"停損觸發:        {ev.get('stop_loss_fires', 0)}",
    ]

    if sa_stats:
        lines += [
            "",
            "━━━━━━ Slot 累計分析 ━━━━━━",
            f"總轉數:     {sa_stats['total_spins']:,}",
            f"勝率:       {sa_stats['win_rate']:.1%}",
            f"EV:         {sa_stats['ev']:.4f}x  (邊際: {sa_stats['edge']:+.2%})",
            f"標準差:     {sa_stats['std_dev']:.4f}",
        ]
        if sa_stats.get("sufficient_data"):
            kf = sa_stats["kelly_fraction"]
            lines.append(f"Kelly f*:   {kf:.4f}  (半 Kelly: {kf/2:.4f})")
        else:
            lines.append(f"Kelly f*:   資料不足（需 {MIN_KELLY_SAMPLES} 筆，"
                         f"目前 {sa_stats['total_spins']}）")

    return "\n".join(lines)


def _record_ts_after(record: dict, since_ts: float) -> bool:
    """history record 的 ts 是 'YYYY-MM-DD HH:MM:SS' 字串；轉 epoch 比對。"""
    ts_str = record.get("ts", "")
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        return dt.timestamp() >= since_ts
    except (ValueError, TypeError):
        return True   # parse 失敗就保守地納入


def _reset_event_counters(state: dict):
    """寄出摘要後 reset 區間計數器（但保留累計 history / slot_analysis）。"""
    state["events"] = {
        "hourly_claims":   0,
        "daily_claims":    0,
        "transfers":       0,
        "neko_completes":  0,
        "stop_loss_fires": 0,
        "goal_hits":       0,
        "bigwins":         0,
        "since_ts":        time.time(),
    }


async def digest_loop(state: dict, config_holder: list):
    """
    每日 email 摘要 loop。
    每次醒來檢查：是否到了 digest_hour 整點？是否啟用？是否本日尚未寄過？
    寄出後 reset 區間計數器、記錄當日 (YYYY-MM-DD) 避免重寄。
    """
    log = logging.getLogger(__name__)
    last_sent_date: str | None = None

    while not state["quit"]:
        # 每分鐘檢查一次（不需要更精準）
        await interruptible_sleep(state, 60)
        if state["quit"]:
            break

        ecfg = config_holder[0].get("email", {})
        if not (ecfg.get("enabled") and ecfg.get("notify_digest", True)):
            continue

        try:
            target_hour = int(ecfg.get("digest_hour", DEFAULT_DIGEST_HOUR))
        except (TypeError, ValueError):
            target_hour = DEFAULT_DIGEST_HOUR
        target_hour = max(0, min(23, target_hour))

        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        # 命中目標小時 + 今日尚未寄
        if now.hour == target_hour and last_sent_date != today:
            try:
                body = _build_digest_body(state)
                ok = await send_email(
                    ecfg, f"[Discord Bot] 📊 每日摘要 {today}", body,
                )
                if ok:
                    log.info("每日摘要 email 已寄出")
                    _log(state, f"📧 每日摘要已寄出 → {ecfg.get('to', '')}")
                    last_sent_date = today
                    _reset_event_counters(state)
                else:
                    log.warning("每日摘要 email 寄出失敗")
            except Exception as e:
                log.error("digest_loop 例外: %s", e)


async def _maybe_notify_goal(page: Page, state: dict, config_holder: list):
    """
    達成 gambling.goal 時：
      1. 送 Discord @ mention
      2. 若 email 已啟用，寄 email
      3. 依 goal_action 處理：
         - "pause"：停用 gambling，等使用者再啟用
         - "raise"：把已達目標設為新門檻，目標 += goal_step；
                    達成後立刻 reset goal_reached，開始追新目標（無限循環）
    """
    log = logging.getLogger(__name__)
    config = config_holder[0]
    gcfg   = config.get("gambling", {})
    goal   = int(gcfg.get("goal", 0) or 0)
    if goal <= 0:
        return
    bal = state["balance"]
    if bal is None:
        return

    if bal >= goal and not state["goal_reached"]:
        state["goal_reached"] = True
        state["events"]["goal_hits"] += 1
        user_id = str(gcfg.get("notify_user_id") or DEFAULT_NOTIFY_USER_ID)
        action  = (gcfg.get("goal_action") or DEFAULT_GOAL_ACTION).lower()
        step    = int(gcfg.get("goal_step", DEFAULT_GOAL_STEP) or 0)

        log.info("達成目標 %d（餘額 %d），動作=%s", goal, bal, action)

        # 1. Discord mention
        try:
            await notify_goal_reached(page, bal, goal, user_id)
        except Exception as e:
            log.warning("Discord mention 失敗: %s", e)

        # 2. Email
        ecfg = config.get("email", {})
        if ecfg.get("enabled") and ecfg.get("notify_goal", True):
            stats = (
                f"目前餘額: {bal:,}\n"
                f"目標餘額: {goal:,}\n"
                f"起始餘額: {state.get('start_balance')}\n"
                f"本次盈虧: {(bal - (state.get('start_balance') or bal)):+,}\n"
                f"總下注: {state['total_bets']}（勝 {state['wins']} / 負 {state['losses']}）\n"
                f"後續動作: {action}"
            )
            await send_email(ecfg, f"[Discord Bot] 達成賭博目標 {goal:,}", stats)

        # 3. 處理後續動作
        if action == "raise" and step > 0:
            new_threshold = goal
            new_goal = goal + step
            gcfg["threshold"] = new_threshold
            gcfg["goal"]      = new_goal
            save_config(config)
            config_holder[0] = load_config()
            state["goal_reached"] = False   # 重置以便追新目標
            log.info("raise 模式：門檻 → %d、目標 → %d", new_threshold, new_goal)
        else:
            # pause 模式（也是預設）：停用賭博，由使用者決定是否再啟用
            gcfg["enabled"] = False
            save_config(config)
            config_holder[0] = load_config()
            log.info("pause 模式：賭博已停用，等候使用者重新啟用")

    elif bal < goal and state["goal_reached"]:
        state["goal_reached"] = False


async def _maybe_handle_stop_loss(state: dict, config_holder: list) -> bool:
    """
    觸發 gambling.loss_floor 時：
      1. 若 email 啟用，寄停損通知
      2. 依 loss_action 處理：
         - "pause"：停用 gambling，等使用者再啟用
         - "lower_threshold"：把 threshold 拉到「當前餘額 - loss_step」（保留緩衝），
                              停損點同步下降到 loss_floor - loss_step。再次跌破才會
                              觸發；不停用 gambling，讓 bot 在低基期繼續嘗試
    回傳 True 表示這次有觸發停損，呼叫端可決定是否跳過下注。
    避免 spam：每次跌破 → 觸發一次 → 餘額回到 loss_floor 之上才 reset。
    """
    log = logging.getLogger(__name__)
    config = config_holder[0]
    gcfg   = config.get("gambling", {})
    floor  = int(gcfg.get("loss_floor", 0) or 0)
    if floor <= 0:
        return False
    bal = state["balance"]
    if bal is None:
        return False

    # 還沒跌破 → 若之前有觸發過、現在已恢復就 reset
    if bal > floor:
        if state.get("loss_triggered"):
            state["loss_triggered"] = False
            log.info("餘額 %d 已回到停損點 %d 以上，loss_triggered 重置", bal, floor)
        return False

    # 已經觸發過、餘額還在停損點以下 → 不重複處理
    if state.get("loss_triggered"):
        return True

    # 第一次觸發
    state["loss_triggered"] = True
    state["events"]["stop_loss_fires"] += 1
    action = (gcfg.get("loss_action") or DEFAULT_LOSS_ACTION).lower()
    step   = int(gcfg.get("loss_step", DEFAULT_LOSS_STEP) or 0)
    log.warning("觸發停損 %d（餘額 %d），動作=%s", floor, bal, action)
    _log(state, f"⛔ 觸發停損 {floor:,}（餘額 {bal:,}），動作={action}")

    # 1. Email
    ecfg = config.get("email", {})
    if ecfg.get("enabled") and ecfg.get("notify_loss", True):
        body = (
            f"目前餘額: {bal:,}\n"
            f"停損點:   {floor:,}\n"
            f"起始餘額: {state.get('start_balance')}\n"
            f"本次盈虧: {(bal - (state.get('start_balance') or bal)):+,}\n"
            f"總下注: {state['total_bets']}（勝 {state['wins']} / 負 {state['losses']}）\n"
            f"後續動作: {action}"
        )
        try:
            await send_email(ecfg, f"[Discord Bot] 觸發停損 {floor:,}", body)
        except Exception as e:
            log.warning("停損 email 寄出失敗: %s", e)

    # 2. 處理後續動作
    if action == "lower_threshold" and step > 0:
        # 把 threshold 拉到「當前餘額 - step」、停損點同步下移
        # 維持「停損點 < threshold」的相對關係，避免立刻又被觸發
        new_threshold = max(0, bal - step)
        new_floor     = max(0, floor - step)
        gcfg["threshold"]  = new_threshold
        gcfg["loss_floor"] = new_floor
        save_config(config)
        config_holder[0] = load_config()
        state["loss_triggered"] = False   # 階梯下移後重置，等再次跌破
        log.info("lower_threshold 模式：門檻 → %d、停損 → %d",
                 new_threshold, new_floor)
        _log(state, f"⛔ 階梯下移：門檻={new_threshold:,} 停損={new_floor:,}")
    else:
        # pause 模式：停用 gambling
        gcfg["enabled"] = False
        save_config(config)
        config_holder[0] = load_config()
        log.info("pause 模式：賭博已停用")
        _log(state, "⛔ 賭博已停用")

    return True


async def gambling_loop(page: Page, state: dict, config_holder: list):
    log = logging.getLogger(__name__)
    fail_count = 0           # 連續失敗計數（簡化單一 counter）
    RECOVER_THRESHOLD = 3    # 累積到此值就 reload 頻道並強制重抓餘額

    async def _check_dead(ctx: str):
        """fail_count 越過 dead_threshold 就寄一次停擺 email；不影響其他流程。"""
        ecfg = config_holder[0].get("email", {})
        thr = int(ecfg.get("dead_threshold", DEFAULT_DEAD_THRESHOLD) or 0)
        if thr > 0 and fail_count >= thr:
            await _notify_dead(state, config_holder, fail_count, context=ctx)

    def _reset_fail_state():
        """成功讀到餘額時呼叫：歸零連續失敗計數、允許下次停擺通知再寄。"""
        nonlocal fail_count
        fail_count = 0
        if state.get("dead_notified"):
            log.info("餘額讀取已恢復，重置 dead_notified")
        state["dead_notified"] = False

    while not state["quit"]:
        await _wait_while_paused(state)
        if state["quit"]:
            break

        gcfg = config_holder[0].get("gambling", {})

        if not gcfg.get("enabled", True):
            await interruptible_sleep(state, 30)
            continue

        balance  = state["balance"]
        threshold = gcfg.get("threshold", 5000)

        # 餘額未知（初始讀取失敗或被清除）→ 短間隔重試
        if balance is None:
            log.info("餘額未知，30 秒後重新查詢")
            await interruptible_sleep(state, 30)
            new = await get_balance(page)
            if new is not None:
                state["balance"] = new
                if state["start_balance"] is None:
                    state["start_balance"] = new
                    ensure_gambling_defaults(config_holder[0], new)
                    config_holder[0] = load_config()
                log.info("已取得餘額: %d 油幣", new)
                _reset_fail_state()
                await _maybe_notify_goal(page, state, config_holder)
            else:
                fail_count += 1
                await _check_dead(f"初始 /balance 連 {fail_count} 次失敗")
                if fail_count >= RECOVER_THRESHOLD:
                    log.warning("連續 %d 次抓不到餘額 → 觸發頻道 reload", fail_count)
                    await recover_page(page, state)
                    re_bal = await get_balance(page)
                    if re_bal is not None:
                        state["balance"] = re_bal
                        log.info("reload 後重抓餘額成功: %d", re_bal)
                        _reset_fail_state()
                    else:
                        fail_count = 0   # 防 reload 風暴；dead_notified 仍保留
            continue

        # 連敗冷靜中 → 等到 cooldown_until_ts 之後才繼續
        cd_until = state.get("cooldown_until_ts")
        if cd_until is not None:
            remain = cd_until - time.time()
            if remain > 0:
                await interruptible_sleep(state, min(remain, 30))
                continue
            else:
                state["cooldown_until_ts"] = None
                state["current_streak"] = 0   # 冷靜結束 → reset streak（避免立刻又觸發）
                _log(state, "😌 冷靜結束，繼續下注")

        # 停損檢查：觸發後可能停用 gambling 或下移門檻；下次 loop 再讀新狀態
        if await _maybe_handle_stop_loss(state, config_holder):
            await interruptible_sleep(state, 30)
            continue

        if balance <= threshold:
            log.info("餘額 %d ≤ %d，等待 %d 分鐘後重查",
                     balance, threshold, GAMBLE_RECHECK_SEC // 60)
            await interruptible_sleep(state, GAMBLE_RECHECK_SEC)
            new = await get_balance(page)
            if new is not None:
                state["balance"] = new
                _reset_fail_state()
                await _maybe_notify_goal(page, state, config_holder)
            continue

        bet = calculate_bet(balance, gcfg, state.get("slot_analysis"))
        if bet <= 0:
            await interruptible_sleep(state, 30)
            continue

        state["current_bet"] = bet
        log.info("餘額 %d > %d，下注 %d", balance, threshold, bet)

        result = await play_slot(page, bet)

        if result is not None:
            new_balance = result["balance"]
            parsed_change = result["change"]

            # 優先用 slot embed 文字解析的結果（不會被 hourly/daily 干擾）；
            # 解析不到才退回餘額差分（最後保險，可能不準）
            if parsed_change is not None:
                change = parsed_change
            else:
                change = new_balance - balance
                log.warning("slot embed 無法解析勝負，用餘額差分（可能因 hourly 干擾不準）")

            state["net_change"] += change
            state["total_bets"] += 1
            if change > 0:
                state["wins"] += 1
                # streak 更新：連勝 +1（如果之前在連敗就重置）
                state["current_streak"] = (
                    state["current_streak"] + 1
                    if state["current_streak"] > 0 else 1
                )
                state["max_win_streak"] = max(
                    state["max_win_streak"], state["current_streak"]
                )
            else:
                state["losses"] += 1
                # streak 更新：連敗 -1（如果之前在連勝就重置）
                state["current_streak"] = (
                    state["current_streak"] - 1
                    if state["current_streak"] < 0 else -1
                )
                state["max_loss_streak"] = max(
                    state["max_loss_streak"], abs(state["current_streak"])
                )
                # 連敗冷靜檢查
                pause_n = int(gcfg.get("loss_streak_pause", 0) or 0)
                if pause_n > 0 and abs(state["current_streak"]) >= pause_n:
                    cooldown_min = float(
                        gcfg.get("loss_streak_cooldown_min", 5) or 0
                    )
                    if cooldown_min > 0:
                        until = time.time() + cooldown_min * 60
                        state["cooldown_until_ts"] = until
                        log.warning(
                            "連敗 %d 場 ≥ %d，暫停下注 %.1f 分鐘",
                            abs(state["current_streak"]), pause_n, cooldown_min,
                        )
                        _log(state, f"😤 連敗 {abs(state['current_streak'])} 場 → 冷靜 {cooldown_min:.0f} 分鐘")
            state["balance"] = new_balance
            _reset_fail_state()
            log.info("結果: %s%d | 餘額: %d | 勝/敗: %d/%d",
                     "+" if change >= 0 else "", change, new_balance,
                     state["wins"], state["losses"])
            state["history"].append({
                "ts":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "bet":    bet,
                "before": new_balance - change,
                "after":  new_balance,
                "change": change,
                "result": "win" if change > 0 else "loss",
                "lines":  result.get("lines", []),
            })

            # 累加 slot 分析資料
            _update_slot_analysis(
                state, bet, change,
                result.get("lines", []), result.get("grid"),
            )
            if state["slot_analysis"]["total_spins"] % 20 == 0:
                save_slot_analysis(state)
                save_history(state)

            # 中大獎通知（改變餘額成功 + 真的解析到 win 才檢查）
            if parsed_change is not None and change > 0 and bet > 0:
                gross_win = change + bet
                multiplier = gross_win / bet
                bigwin_threshold = float(
                    gcfg.get("bigwin_multiplier", DEFAULT_BIGWIN_MULTIPLIER) or 0
                )
                if bigwin_threshold > 0 and multiplier >= bigwin_threshold:
                    await _notify_bigwin(state, config_holder,
                                         bet, gross_win, multiplier)

            await _maybe_notify_goal(page, state, config_holder)
        else:
            fail_count += 1
            log.warning("無法解析餘額（連續第 %d 次失敗）", fail_count)
            await _check_dead(f"/slot 連 {fail_count} 次失敗")

            # 階梯式恢復：第 2 次先試 /balance 補抓；第 3 次直接 reload 頻道
            if fail_count == 2:
                log.info("試 /balance 重新對齊餘額")
                new = await get_balance(page)
                if new is not None:
                    state["balance"] = new
                    log.info("/balance 取得餘額: %d", new)
                    _reset_fail_state()
            elif fail_count >= RECOVER_THRESHOLD:
                log.warning("連續 %d 次失敗 → 觸發頻道 reload", fail_count)
                ok = await recover_page(page, state)
                if ok:
                    re_bal = await get_balance(page)
                    if re_bal is not None:
                        state["balance"] = re_bal
                        log.info("reload 後重抓餘額成功: %d", re_bal)
                        _reset_fail_state()
                    else:
                        fail_count = 0   # 防 reload 風暴；dead_notified 仍保留
                else:
                    fail_count = 0   # 同上

        # 用 config 設定的間距休息
        i_min = float(gcfg.get("interval_min", DEFAULT_INTERVAL_MIN))
        i_max = float(gcfg.get("interval_max", DEFAULT_INTERVAL_MAX))
        if i_max < i_min:
            i_max = i_min
        await interruptible_sleep(state, random.uniform(i_min, i_max))


# ── UI 迴圈 ───────────────────────────────────────────────────────────────────
def _log(state: dict, msg: str):
    state["log_lines"].append(f"{datetime.now():%H:%M:%S} {msg}")
    if len(state["log_lines"]) > 25:
        state["log_lines"].pop(0)


async def ui_loop(state: dict, config_holder: list, page: Page | None = None):
    with Live(
        build_layout(state, config_holder[0]),
        console=console,
        refresh_per_second=2,
        screen=True,
    ) as live:
        while not state["quit"]:
            key = state.get("pending_key")
            if key:
                state["pending_key"] = None
                if key == "q":
                    state["quit"] = True
                    break
                elif key == "c":
                    live.stop()
                    try:
                        await run_config_menu(state, config_holder)
                    finally:
                        live.start()
                elif key == "p":
                    state["paused"] = not state.get("paused", False)
                    _log(state, "已暫停所有功能（再按 P 恢復）"
                         if state["paused"] else "已恢復運行")
                elif key == "e":
                    csv_path = export_history_csv(state)
                    png_path = export_history_chart(state)
                    analysis_path = export_slot_analysis(state)
                    if csv_path is None and analysis_path is None:
                        _log(state, "尚無賭博紀錄可匯出")
                    else:
                        if csv_path:
                            _log(state, f"CSV 已匯出: {csv_path}")
                        if png_path:
                            _log(state, f"圖表已匯出: {png_path}")
                        elif csv_path:
                            _log(state, "（未安裝 matplotlib，跳過圖表）")
                        if analysis_path:
                            _log(state, f"分析已匯出: {analysis_path}")
                elif key == "s":
                    live.stop()
                    try:
                        _show_slot_analysis(state)
                    finally:
                        live.start()
                elif key == "f":
                    # 整個程式重啟（透過 exit code 由 run.bat 偵測）
                    _log(state, "已請求重啟，正在收尾...")
                    state["reboot"] = True
                    state["quit"]   = True
                    break
                elif key == "w":
                    # 在預設瀏覽器打開 dashboard
                    url = _dashboard_local_url(config_holder[0])
                    try:
                        import webbrowser
                        webbrowser.open(url)
                        _log(state, f"🌐 已在瀏覽器打開 {url}")
                    except Exception as e:
                        _log(state, f"⚠ 開啟瀏覽器失敗: {e}")
                elif key == "k":
                    # 複製 dashboard 的 LAN URL 到剪貼簿（給手機用）
                    lan_url = _dashboard_lan_url(config_holder[0])
                    if _copy_to_clipboard(lan_url):
                        _log(state, f"📋 已複製: {lan_url}")
                    else:
                        _log(state, f"⚠ 複製失敗（手動複製: {lan_url}）")

            live.update(build_layout(state, config_holder[0]))
            await asyncio.sleep(0.5)


# ── 主程式 ────────────────────────────────────────────────────────────────────
# ── 首次設定 / 登入精靈 ────────────────────────────────────────────────────
def _is_placeholder_or_missing(value: str | None, placeholder_keywords=()) -> bool:
    """判斷 config 欄位是否為空 / 還是範本佔位符。"""
    if not value:
        return True
    s = str(value)
    if not s.strip():
        return True
    return any(k in s for k in placeholder_keywords)


def _config_needs_setup(cfg: dict) -> list[str]:
    """檢查 config 哪些欄位還沒填好；回傳待補欄位清單。"""
    missing = []
    if _is_placeholder_or_missing(cfg.get("guild_id"), ["YOUR_", "HERE"]):
        missing.append("guild_id")
    if _is_placeholder_or_missing(cfg.get("channel_id"), ["YOUR_", "HERE"]):
        missing.append("channel_id")
    g = cfg.get("gambling", {}) or {}
    if _is_placeholder_or_missing(
        g.get("notify_user_id"),
        ["YOUR_", "HERE", DEFAULT_NOTIFY_USER_ID],   # 預設值也算「沒設」
    ):
        missing.append("notify_user_id")
    return missing


def _ensure_config_via_wizard():
    """
    若 config.json 不存在 → 從 config.example.json 複製。
    若有缺欄位 → 互動式提示使用者輸入。
    """
    if not os.path.exists(CONFIG_PATH):
        if os.path.exists("config.example.json"):
            print(f"\n首次啟動 — 從 config.example.json 複製為 {CONFIG_PATH}")
            with open("config.example.json", encoding="utf-8") as src, \
                 open(CONFIG_PATH, "w", encoding="utf-8") as dst:
                dst.write(src.read())
        else:
            print(f"⚠ 找不到 {CONFIG_PATH} 也找不到 config.example.json")
            print("  請手動建立 config.json 後重啟。")
            return False

    cfg = load_config()
    missing = _config_needs_setup(cfg)
    if not missing:
        return True

    print()
    print("=" * 64)
    print("  🛠️  首次設定 — 請填入下列資訊（按 Enter 跳過保留現值）")
    print("=" * 64)
    print()
    print("  📌 開啟 Discord 開發者模式：使用者設定 → 進階 → 啟用「開發者模式」")
    print("      之後右鍵伺服器/頻道/使用者就會多出「複製 ID」選項")
    print()

    if "guild_id" in missing:
        cur = cfg.get("guild_id", "")
        cur_disp = "未設定" if not cur or "YOUR_" in str(cur) else cur
        print(f"  【伺服器 ID】(目前: {cur_disp})")
        print("    → 對伺服器右鍵 → 複製伺服器 ID")
        raw = input("    伺服器 ID: ").strip()
        if raw.isdigit():
            cfg["guild_id"] = raw

    if "channel_id" in missing:
        cur = cfg.get("channel_id", "")
        cur_disp = "未設定" if not cur or "YOUR_" in str(cur) else cur
        print(f"\n  【頻道 ID】(目前: {cur_disp}) — bot 會在此頻道送指令")
        print("    → 對要操作的頻道右鍵 → 複製頻道 ID")
        raw = input("    頻道 ID: ").strip()
        if raw.isdigit():
            cfg["channel_id"] = raw

    if "notify_user_id" in missing:
        g = cfg.setdefault("gambling", {})
        cur = g.get("notify_user_id", "")
        cur_disp = ("未設定" if not cur or "YOUR_" in str(cur)
                    or cur == DEFAULT_NOTIFY_USER_ID else cur)
        print(f"\n  【通知對象 User ID】(目前: {cur_disp})")
        print("    → 達成目標 / 貓娘完成時要 @ 的對象（通常填自己）")
        print("    → 對使用者右鍵 → 複製使用者 ID")
        raw = input("    User ID: ").strip()
        if raw.isdigit():
            g["notify_user_id"] = raw

    save_config(cfg)
    print("\n  ✓ 設定已儲存。")

    # 再檢查一次，若仍有空欄位提醒
    cfg = load_config()
    still = _config_needs_setup(cfg)
    if still:
        print(f"\n  ⚠ 仍有未填欄位: {', '.join(still)}")
        print("  bot 可能無法正常啟動。可隨時編輯 config.json 後重新執行 run.bat。")
    print("=" * 64)
    return True


async def _run_login_wizard():
    """
    沒有 storage_state.json → 開啟 Chromium 引導使用者手動登入 Discord。
    把原本 login.py 的邏輯內嵌進來，省得使用者要先跑 login.bat。
    """
    print()
    print("=" * 64)
    print("  🔐 Discord 登入 — 找不到或 storage_state.json 已過期")
    print("=" * 64)
    print()
    print("  即將開啟 Chromium 視窗，請手動完成 Discord 登入（含 2FA）。")
    print("  網址跳轉到 /channels/... 時會自動關閉並儲存登入狀態。")
    print("  （登入逾時 5 分鐘）")
    print()
    input("  按 Enter 繼續...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://discord.com/login")
        try:
            await page.wait_for_url("**/channels/**", timeout=300_000)
            print("\n  ✓ 登入成功！儲存 session 中...")
            await context.storage_state(path=STORAGE_STATE_PATH)
            print(f"  ✓ 已儲存至 {STORAGE_STATE_PATH}")
        finally:
            await browser.close()


async def main():
    # 第一次啟動或 config 不完整 → 引導設定
    if not _ensure_config_via_wizard():
        return

    # 沒有 storage_state.json → 引導登入
    if not os.path.exists(STORAGE_STATE_PATH):
        await _run_login_wizard()
        if not os.path.exists(STORAGE_STATE_PATH):
            print("\n⚠ 登入流程未完成，程式中止。")
            return

    state         = make_state()
    initial_cfg   = load_config()
    log           = setup_logging(
        state,
        log_level=initial_cfg.get("log_level", "INFO"),
    )
    config_holder = [initial_cfg]
    global config_holder_ref
    config_holder_ref = config_holder    # 給 recover_page 寄信用
    start_kb_listener(state)

    config     = config_holder[0]
    guild_id   = config["guild_id"]
    channel_id = config["channel_id"]
    state["guild_id"]   = guild_id
    state["channel_id"] = channel_id

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=STORAGE_STATE_PATH)
        page    = await context.new_page()

        try:
            await navigate_to_channel(page, guild_id, channel_id)
        except RuntimeError as e:
            # session 過期或頻道載不到 → 關掉 browser，引導重新登入
            await browser.close()
            if "session 已過期" in str(e):
                # 砍掉舊 session，引導重新登入再重啟
                if os.path.exists(STORAGE_STATE_PATH):
                    os.remove(STORAGE_STATE_PATH)
                await _run_login_wizard()
                # 登入完讓使用者用 F 鍵或重新跑 run.bat。直接 sys.exit(REBOOT_EXIT_CODE)
                # 通知 run.bat 重新啟動
                print("\n  ✓ 重新登入完成，3 秒後自動重啟 bot...")
                await asyncio.sleep(3)
                sys.exit(REBOOT_EXIT_CODE)
            else:
                # 其他錯誤（網路 / ID 設定錯）— 讓使用者看到訊息後退出
                raise

        # 序列化啟動：先讀初始餘額，避免和其他 loop 的指令搶 lock 造成 race
        log.info("等待聊天歷史載入...")
        await asyncio.sleep(3)

        # 先送 /balance 查詢；失敗才回退到聊天歷史
        log.info("送 /balance 查詢初始餘額")
        balance = await get_balance(page)
        if balance is not None:
            log.info("從 /balance 取得餘額: %d 油幣", balance)
        else:
            log.warning("/balance 沒回應，回退到從聊天歷史搜尋")
            balance = await read_initial_balance_from_history(page)
            if balance is not None:
                log.info("從歷史訊息讀到餘額: %d 油幣", balance)
            else:
                log.warning("初始餘額讀取失敗，gambling_loop 會自動重試")

        state["balance"] = balance
        state["start_balance"] = balance
        ensure_gambling_defaults(config_holder[0], balance)
        config_holder[0] = load_config()

        # 載入持久化的 slot 分析資料
        persisted_sa = load_slot_analysis()
        if persisted_sa:
            state["slot_analysis"] = persisted_sa
            log.info("已載入 slot 分析資料（%d 筆紀錄）",
                     persisted_sa.get("total_spins", 0))

        # 載入持久化的下注歷史紀錄（讓 E 鍵的圖表匯出可跨 session 使用）
        persisted_history = load_history()
        if persisted_history:
            state["history"] = persisted_history
            log.info("已載入下注歷史紀錄（%d 筆）", len(persisted_history))

        state["status"] = "運行中"

        # Web dashboard — 開在背景 thread（不是 asyncio task；用 stdlib http.server）
        dashboard_thread = None
        dcfg = config_holder[0].get("dashboard", {})
        if dcfg.get("enabled", True):
            try:
                from bot.web.dashboard import start_dashboard_thread

                def _dashboard_action(action: str) -> dict:
                    """從 dashboard 控制台觸發的動作。回傳 {ok, message}。"""
                    if action == "toggle_pause":
                        state["paused"] = not state.get("paused", False)
                        msg = "已暫停" if state["paused"] else "已恢復"
                        _log(state, f"📡 Dashboard：{msg}")
                        return {"ok": True, "message": msg}
                    elif action == "reset_analysis":
                        n = state.get("slot_analysis", {}).get("total_spins", 0)
                        state["slot_analysis"] = _make_slot_analysis()
                        if os.path.exists(ANALYSIS_PATH):
                            os.remove(ANALYSIS_PATH)
                        _log(state, f"📡 Dashboard：重置分析（{n} 筆清除）")
                        return {"ok": True, "message": f"已重置（{n} 筆清除）"}
                    elif action == "restart":
                        state["reboot"] = True
                        state["quit"] = True
                        _log(state, "📡 Dashboard：請求重啟")
                        return {"ok": True, "message": "重啟請求已送出，3 秒後重啟"}
                    else:
                        return {"ok": False, "message": f"未知動作: {action}"}

                dashboard_thread = start_dashboard_thread(
                    state, config_holder,
                    on_action=_dashboard_action,
                    host=dcfg.get("host", "0.0.0.0"),
                    port=int(dcfg.get("port", 8765)),
                )
                if dashboard_thread:
                    log.info("Web dashboard 啟動在 http://%s:%d/",
                             dcfg.get("host", "0.0.0.0"),
                             int(dcfg.get("port", 8765)))
            except Exception as e:
                log.warning("Web dashboard 啟動失敗: %s", e)

        ui_task = asyncio.create_task(ui_loop(state, config_holder, page))
        worker_tasks = [
            asyncio.create_task(hourly_loop(page, state, config_holder)),
            asyncio.create_task(daily_loop(page, state, config_holder)),
            asyncio.create_task(gambling_loop(page, state, config_holder)),
            asyncio.create_task(nekomusume_loop(page, state, config_holder)),
            asyncio.create_task(transfer_loop(page, state, config_holder)),
            asyncio.create_task(digest_loop(state, config_holder)),
        ]

        await ui_task   # 等待使用者按 Q 離開

        for t in worker_tasks:
            t.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)

        # 關 dashboard server（在 worker tasks 取消後再做，避免 race）
        if dashboard_thread is not None:
            try:
                from bot.web.dashboard import stop_dashboard_thread
                stop_dashboard_thread(dashboard_thread)
            except Exception:
                pass

        await browser.close()
        save_slot_analysis(state)
        save_history(state)
        log.info("程式已結束（slot 分析 / 歷史紀錄已儲存）")

        if state.get("reboot"):
            log.info("Reboot 已請求，以 exit code %d 退出讓 run.bat 重啟", REBOOT_EXIT_CODE)
            sys.exit(REBOOT_EXIT_CODE)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

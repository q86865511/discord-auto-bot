"""
Discord 自動指令腳本 - 含 Rich 終端 UI
"""
import asyncio
import csv
import json
import logging
import msvcrt
import os
import random
import re
import smtplib
import sys
import threading
import time
from datetime import datetime
from email.mime.text import MIMEText

from playwright.async_api import async_playwright, Page
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ── 常數 ──────────────────────────────────────────────────────────────────────
CONFIG_PATH = "config.json"
STORAGE_STATE_PATH = "storage_state.json"
EXPORT_DIR = "exports"
HOURLY_BASE_SEC   = 3600
HOURLY_JITTER_SEC = 8 * 60
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
DEFAULT_NEKOMUSUME_INTERVAL_MIN = 30   # /check 監控間距
REBOOT_EXIT_CODE = 42     # main 退出時用這個碼通知 run.bat 重新啟動

command_lock = asyncio.Lock()
console = Console()


# ── 共享狀態 ──────────────────────────────────────────────────────────────────
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
        "neko_status": "unknown",   # dispatching / not_dispatching / unknown
        "neko_remaining_min": None,
        "session_start_ts": time.time(),
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


def setup_logging(state: dict) -> logging.Logger:
    root = logging.getLogger()
    root.handlers.clear()
    h = UILogHandler(state)
    h.setLevel(logging.INFO)
    root.addHandler(h)
    root.setLevel(logging.INFO)
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
        writer.writerow(["時間", "下注", "下注前餘額", "下注後餘額", "變動", "結果"])
        for r in history:
            writer.writerow([
                r["ts"], r["bet"], r["before"], r["after"], r["change"], r["result"],
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


def save_config(config: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def ensure_gambling_defaults(config: dict, balance: int | None = None):
    """補齊 gambling / email / nekomusume 欄位預設值（只填未設定的欄位）。"""
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

    ncfg = config.setdefault("nekomusume", {})
    ncfg.setdefault("enabled",          True)
    ncfg.setdefault("check_interval_min", DEFAULT_NEKOMUSUME_INTERVAL_MIN)

    save_config(config)


# ── 押注策略 ──────────────────────────────────────────────────────────────────
def calculate_bet(balance: int, gcfg: dict) -> int:
    threshold = gcfg.get("threshold", 5000)
    min_bet   = gcfg.get("min_bet",   100)
    max_bet   = gcfg.get("max_bet",   500)
    fraction  = gcfg.get("bet_fraction", 0.02)
    strategy  = gcfg.get("strategy",  "auto")

    excess = balance - threshold
    if excess <= 0:
        return 0

    bet = min_bet if strategy == "fixed" else max(min_bet, int(excess * fraction))
    if max_bet > 0:
        bet = min(bet, max_bet)
    return min(bet, excess)   # 安全上限：不讓餘額低於門檻


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

    # Header
    header = Panel(
        Text(f"🤖 Discord Auto Bot  |  [{status_color}]{status_text}[/{status_color}]",
             justify="center"),
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

    t1 = Table(box=None, show_header=False, padding=(0, 2), expand=True)
    t1.add_column(style="dim", width=14)
    t1.add_column()
    t1.add_row("💰 目前餘額",  bal_str)
    t1.add_row("📌 起始餘額",  start_str)
    t1.add_row("📈 本次盈虧",  diff_str)
    t1.add_row("🏁 目標進度",  goal_str)
    t1.add_row("", "")
    t1.add_row("🎲 總下注",    str(total_bets))
    t1.add_row("✅ 獲勝",      f"[green]{state['wins']}[/green]")
    t1.add_row("❌ 失敗",      f"[red]{state['losses']}[/red]")
    t1.add_row("📊 勝率",      f"{win_rate:.1f}%")
    t1.add_row("💵 賭博淨收",  net_str)
    t1.add_row("", "")
    t1.add_row("🎯 當前下注",
               f"[yellow]{cur_bet:,}[/yellow]" if cur_bet else "─")
    stats_panel = Panel(t1, title="[bold]📊 統計[/bold]", border_style="blue")

    # 設定面板
    strategy    = gcfg.get("strategy", "auto")
    frac_pct    = f"{gcfg.get('bet_fraction', 0.02) * 100:.1f}%"
    max_bet     = gcfg.get("max_bet", 0)
    max_bet_str = f"{max_bet:,}" if max_bet > 0 else "自動"
    strat_label = f"[cyan]{strategy}[/cyan]" + (f" ({frac_pct})" if strategy == "auto" else "")

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
    neko_min = state.get("neko_remaining_min")
    if neko_st == "dispatching":
        if neko_min is None:
            neko_str = "[yellow]派遣中[/yellow]"
        elif neko_min >= 60:
            neko_str = f"[yellow]派遣中 ({neko_min // 60}h{neko_min % 60:02d}m)[/yellow]"
        else:
            neko_str = f"[yellow]派遣中 ({neko_min}m)[/yellow]"
    elif neko_st == "not_dispatching":
        neko_str = "[green]待領取/閒置[/green]"
    else:
        neko_str = "[dim]─[/dim]"
    t2.add_row("🐱 貓娘",     neko_str)

    cfg_panel = Panel(t2, title="[bold]⚙️ 設定[/bold]  [dim]C:修改 R:重載[/dim]",
                      border_style="green")

    # 日誌
    lines    = state["log_lines"][-10:]
    log_text = "\n".join(lines) if lines else "[dim]尚無日誌[/dim]"
    log_panel = Panel(log_text, title="[bold]📋 日誌[/bold]", border_style="dim", height=13)

    # Footer
    pause_label = "[yellow]P 恢復[/yellow]" if state.get("paused") else "[bold]P[/bold] 暫停"
    footer = Panel(
        f"[dim][bold]Q[/bold] 退出  [bold]C[/bold] 修改設定  [bold]R[/bold] 重載  "
        f"{pause_label}  [bold]E[/bold] 匯出  [bold]L[/bold] 重載頻道  [bold]F[/bold] 重啟程式[/dim]",
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


async def run_config_menu(state: dict, config_holder: list):
    os.system("cls")
    config = config_holder[0]
    gcfg   = config.setdefault("gambling", {})

    ecfg = config.setdefault("email",       {})
    ncfg = config.setdefault("nekomusume",  {})

    while True:
        i_min  = gcfg.get('interval_min', DEFAULT_INTERVAL_MIN)
        i_max  = gcfg.get('interval_max', DEFAULT_INTERVAL_MAX)
        goal   = gcfg.get('goal', DEFAULT_GOAL)
        uid    = gcfg.get('notify_user_id', DEFAULT_NOTIFY_USER_ID)
        action = gcfg.get('goal_action', DEFAULT_GOAL_ACTION)
        step   = gcfg.get('goal_step', DEFAULT_GOAL_STEP)
        em_on  = ecfg.get('enabled', False)
        em_to  = ecfg.get('to', '')
        nk_on  = ncfg.get('enabled', True)

        print(f"\n{'═'*48}")
        print("  ⚙️  Discord Bot — 設定修改")
        print(f"{'═'*48}")
        print("  [賭博]")
        print(f"   [1] 保底門檻:    {gcfg.get('threshold', 5000):,}")
        print(f"   [2] 最小下注:    {gcfg.get('min_bet', 100):,}")
        print(f"   [3] 最大下注:    {gcfg.get('max_bet', 500):,}  (0 = 自動)")
        print(f"   [4] 押注比例:    {gcfg.get('bet_fraction', 0.02)*100:.1f}%  (auto策略用)")
        print(f"   [5] 策略:        {gcfg.get('strategy', 'auto')}  (auto / fixed)")
        print(f"   [6] 賭博:        {'啟用' if gcfg.get('enabled') else '停用'}")
        print(f"   [7] 下注間距:    {i_min}-{i_max} 秒")
        print("  [目標]")
        print(f"   [8] 目標餘額:    {goal:,}  (0 = 不設目標)")
        print(f"   [9] 通知 UID:    {uid}")
        print(f"   [A] 達標行為:    {action}  (pause = 停用; raise = 提升門檻續跑)")
        print(f"   [B] raise 步進:  {step:,}  (達標後 新目標 = 舊目標 + 步進)")
        print("  [Email 通知]")
        print(f"   [C] Email:       {'啟用' if em_on else '停用'}  收件人={em_to or '(未設定)'}")
        print(f"   [D] SMTP 設定 (host / port / user / password)")
        print("  [貓娘監控]")
        print(f"   [E] 貓娘監控:    {'啟用' if nk_on else '停用'}  (派遣完成自動 @ 通知)")
        print(f"   [F] 檢查間距:    {ncfg.get('check_interval_min', DEFAULT_NEKOMUSUME_INTERVAL_MIN)} 分鐘")
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
            raw = (await ainput("  策略 (auto / fixed): ")).strip().lower()
            if raw in ("auto", "fixed"):
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

    config["gambling"]   = gcfg
    config["email"]      = ecfg
    config["nekomusume"] = ncfg
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


def _parse_balance_int(s: str) -> int | None:
    try:
        return int(s.replace(',', '').replace('，', ''))
    except ValueError:
        return None


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


# slot 勝/負 marker（從 embed 文字直接判定這局結果，避免和 hourly/daily 的餘額變動混淆）
SLOT_WIN_PATTERN  = re.compile(r'總計贏得[\s:：]*([0-9,，]+)')
SLOT_LOSS_PATTERN = re.compile(r'損失\s*([0-9,，]+)')


def _parse_slot_change(text: str, bet: int) -> int | None:
    """
    從整頁文字找最後一個 slot 結果並計算這局淨變動：
      - 「總計贏得：X」→ change = X - bet（X 是 gross win，包含原本下注）
      - 「什麼都沒中 損失 X」→ change = -X
    回傳 None 表示沒解析到（embed 還沒渲染，或格式變了）。
    用「最後出現位置」鎖定最新一局，避免讀到舊紀錄。
    """
    last_change = None
    last_pos = -1
    for m in SLOT_WIN_PATTERN.finditer(text):
        if m.start() > last_pos:
            v = _parse_balance_int(m.group(1))
            if v is not None:
                last_change = v - bet
                last_pos = m.start()
    for m in SLOT_LOSS_PATTERN.finditer(text):
        if m.start() > last_pos:
            v = _parse_balance_int(m.group(1))
            if v is not None:
                last_change = -v
                last_pos = m.start()
    return last_change


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
        before_count = _count_balance_mentions(before_text)

        await _send_slash_command(page, command, param)

        deadline = time.time() + timeout
        last_val: int | None = None
        last_change_time: float = time.time()
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            current_text = await page.evaluate("() => document.body.textContent")
            current_count = _count_balance_mentions(current_text)
            if current_count <= before_count:
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
        before_text = await page.evaluate("() => document.body.textContent")
        before_count = _count_balance_mentions(before_text)

        await _send_slash_command(page, "/slot", param=str(bet))

        deadline = time.time() + timeout
        last_val: int | None = None
        last_change_time: float = time.time()
        last_text = before_text
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            current_text = await page.evaluate("() => document.body.textContent")
            last_text = current_text
            current_count = _count_balance_mentions(current_text)
            if current_count <= before_count:
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
                return {"balance": val, "change": change}

        if last_val is not None:
            change = _parse_slot_change(last_text, bet)
            log.info("/slot %d timeout 但已抓到值 %d (change=%s)", bet, last_val, change)
            return {"balance": last_val, "change": change}
        log.warning("/slot %d 在 %.0fs 內未取得新餘額", bet, timeout)
        return None


async def navigate_to_channel(page: Page, guild_id: str, channel_id: str):
    log = logging.getLogger(__name__)
    url = f"https://discord.com/channels/{guild_id}/{channel_id}"
    log.info("導航至頻道: %s", url)
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_selector('[data-slate-editor="true"]', timeout=30_000)
    log.info("頻道已載入")


async def recover_page(page: Page, state: dict) -> bool:
    """
    page state 變糟（連續多次 timeout）時重新載入頻道。
    回傳是否復原成功。整段在 command_lock 內，避免和其他 loop 撞。
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
            return True
        except Exception as e:
            log.error("頻道復原失敗: %s", e)
            return False


# ── 排程迴圈 ──────────────────────────────────────────────────────────────────
async def _wait_while_paused(state: dict):
    """暫停期間原地停留；恢復或退出時返回。"""
    while state.get("paused") and not state["quit"]:
        await asyncio.sleep(0.5)


async def hourly_loop(page: Page, state: dict):
    log = logging.getLogger(__name__)
    while not state["quit"]:
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
            log.info("/hourly 完成，餘額更新為 %d", new_bal)
        else:
            log.info("/hourly 已送出（未取得新餘額，可能尚未到時間）")
        delay = HOURLY_BASE_SEC + random.uniform(-HOURLY_JITTER_SEC, HOURLY_JITTER_SEC)
        state["hourly_next"] = time.time() + delay
        await interruptible_sleep(state, delay)


async def daily_loop(page: Page, state: dict):
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
            log.info("/daily 完成，餘額更新為 %d", new_bal)
        else:
            log.info("/daily 已送出（未取得新餘額，可能尚未到時間）")
        delay = DAILY_BASE_SEC + random.uniform(-DAILY_JITTER_SEC, DAILY_JITTER_SEC)
        state["daily_next"] = time.time() + delay
        await interruptible_sleep(state, delay)


async def _read_check_response(page: Page, timeout: float = 15.0) -> str | None:
    """送 /check（ephemeral），等回應出現「貓娘派遣」字樣後回傳新增文字。"""
    log = logging.getLogger(__name__)
    async with command_lock:
        before = await page.evaluate("() => document.body.textContent")
        before_len = len(before)
        await _send_slash_command(page, "/check")
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            current = await page.evaluate("() => document.body.textContent")
            new_part = current[before_len:] if len(current) >= before_len else current
            if "貓娘派遣" in new_part:
                await asyncio.sleep(0.5)
                current = await page.evaluate("() => document.body.textContent")
                new_part = current[before_len:] if len(current) >= before_len else current
                return new_part
    log.warning("/check 在 %.0fs 內未取得回應", timeout)
    return None


async def nekomusume_loop(page: Page, state: dict, config_holder: list):
    """
    定期送 /check 查貓娘派遣狀態；當狀態從「派遣中」轉為「不在派遣中」時 @ 通知。
    自適應睡眠：剩 >1 小時就先睡到剩 30 分鐘前；快到時則 5-10 分鐘輪詢一次。
    """
    log = logging.getLogger(__name__)
    last_status: str | None = None

    while not state["quit"]:
        await _wait_while_paused(state)
        if state["quit"]:
            break

        ncfg = config_holder[0].get("nekomusume", {})
        if not ncfg.get("enabled", True):
            await interruptible_sleep(state, 300)
            continue

        text = await _read_check_response(page)
        if text is None:
            await interruptible_sleep(state, 600)
            continue

        new_status, minutes = parse_dispatch_status(text)
        state["neko_status"] = new_status
        state["neko_remaining_min"] = minutes
        log.info("貓娘狀態: %s%s", new_status,
                 f"（剩 {minutes} 分鐘）" if minutes is not None else "")

        # 派遣中 → 不在派遣中：通知使用者
        if last_status == "dispatching" and new_status != "dispatching":
            user_id = str(config_holder[0].get("gambling", {})
                          .get("notify_user_id", DEFAULT_NOTIFY_USER_ID))
            try:
                await send_message(
                    page,
                    f"<@{user_id}> 貓娘派遣已完成！記得 `/nekomusume claim` 領取戰利品",
                )
                log.info("已送出貓娘完成通知")
            except Exception as e:
                log.warning("貓娘完成通知失敗: %s", e)

            ecfg = config_holder[0].get("email", {})
            if ecfg.get("enabled"):
                await send_email(
                    ecfg, "[Discord Bot] 貓娘派遣已完成",
                    "貓娘派遣已完成，請至 Discord 用 /nekomusume claim 領取。",
                )

        last_status = new_status

        # 自適應睡眠
        base_min = float(ncfg.get("check_interval_min", DEFAULT_NEKOMUSUME_INTERVAL_MIN))
        if new_status == "dispatching" and minutes is not None:
            if minutes > 60:
                sleep_min = max(base_min, minutes - 30)
            else:
                sleep_min = max(5.0, min(base_min, 10.0))
        else:
            sleep_min = base_min
        await interruptible_sleep(state, sleep_min * 60)


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
        if ecfg.get("enabled"):
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


async def gambling_loop(page: Page, state: dict, config_holder: list):
    log = logging.getLogger(__name__)
    parse_fail = 0           # 連續無法讀餘額/結果
    consecutive_fail = 0     # 連續整體失敗（用於觸發 page recovery）
    RECOVER_THRESHOLD = 5

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
                consecutive_fail = 0
            else:
                consecutive_fail += 1
                if consecutive_fail >= RECOVER_THRESHOLD:
                    await recover_page(page, state)
                    consecutive_fail = 0
            continue

        if balance <= threshold:
            log.info("餘額 %d ≤ %d，等待 %d 分鐘後重查",
                     balance, threshold, GAMBLE_RECHECK_SEC // 60)
            await interruptible_sleep(state, GAMBLE_RECHECK_SEC)
            new = await get_balance(page)
            if new is not None:
                state["balance"] = new
            parse_fail = 0
            continue

        bet = calculate_bet(balance, gcfg)
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
            else:
                state["losses"] += 1
            state["balance"] = new_balance
            parse_fail = 0
            consecutive_fail = 0
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
            })
            await _maybe_notify_goal(page, state, config_holder)
        else:
            parse_fail += 1
            consecutive_fail += 1
            log.warning("無法解析餘額（第 %d 次）", parse_fail)
            if parse_fail >= 2:
                new = await get_balance(page)
                if new is not None:
                    state["balance"] = new
                    consecutive_fail = 0
                parse_fail = 0
            if consecutive_fail >= RECOVER_THRESHOLD:
                await recover_page(page, state)
                consecutive_fail = 0

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
                elif key == "r":
                    config_holder[0] = load_config()
                    _log(state, "設定已從檔案重載")
                elif key == "p":
                    state["paused"] = not state.get("paused", False)
                    _log(state, "已暫停所有功能（再按 P 恢復）"
                         if state["paused"] else "已恢復運行")
                elif key == "e":
                    csv_path = export_history_csv(state)
                    png_path = export_history_chart(state)
                    if csv_path is None:
                        _log(state, "尚無賭博紀錄可匯出")
                    else:
                        _log(state, f"CSV 已匯出: {csv_path}")
                        if png_path:
                            _log(state, f"圖表已匯出: {png_path}")
                        else:
                            _log(state, "（未安裝 matplotlib，跳過圖表）")
                elif key == "f":
                    # 整個程式重啟（透過 exit code 由 run.bat 偵測）
                    _log(state, "已請求重啟，正在收尾...")
                    state["reboot"] = True
                    state["quit"]   = True
                    break
                elif key == "l":
                    # 只重新載入頻道頁面
                    if page is None:
                        _log(state, "頁面參考遺失，無法 reload")
                    else:
                        live.stop()
                        try:
                            ok = await recover_page(page, state)
                            _log(state, "頻道已重新載入" if ok else "頻道重新載入失敗")
                        finally:
                            live.start()

            live.update(build_layout(state, config_holder[0]))
            await asyncio.sleep(0.5)


# ── 主程式 ────────────────────────────────────────────────────────────────────
async def main():
    if not os.path.exists(STORAGE_STATE_PATH):
        print("找不到 storage_state.json！請先執行 login.py 完成登入。")
        return

    state         = make_state()
    log           = setup_logging(state)
    config_holder = [load_config()]
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

        await navigate_to_channel(page, guild_id, channel_id)

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

        state["status"] = "運行中"

        ui_task = asyncio.create_task(ui_loop(state, config_holder, page))
        worker_tasks = [
            asyncio.create_task(hourly_loop(page, state)),
            asyncio.create_task(daily_loop(page, state)),
            asyncio.create_task(gambling_loop(page, state, config_holder)),
            asyncio.create_task(nekomusume_loop(page, state, config_holder)),
        ]

        await ui_task   # 等待使用者按 Q 離開

        for t in worker_tasks:
            t.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)
        await browser.close()
        log.info("程式已結束")

        if state.get("reboot"):
            log.info("Reboot 已請求，以 exit code %d 退出讓 run.bat 重啟", REBOOT_EXIT_CODE)
            sys.exit(REBOOT_EXIT_CODE)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

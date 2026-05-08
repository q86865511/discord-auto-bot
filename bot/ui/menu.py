"""互動式設定選單 — 全部使用 input_validation 防呆。

設計:
- 所有輸入走 ask_int / ask_float / ask_choice / ask_user_id 等,自動拒絕中文 / 全形 / 範圍外
- 每次「儲存並返回」會跑 schema.validate(),不通過就顯示錯誤並讓使用者修正
- 敏感欄位(密碼)不會顯示明文,只顯示「已設定 / 未設定」
- 子選單分:賭博 / 目標停損 / Email / 貓娘 / 轉帳 / Dashboard / 進階(在 maintenance.py)

入口:run_config_menu。

first_run_wizard 已搬到 bot.ui.wizard,這裡只 re-export 保留舊呼叫者相容性。
"""
from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from bot.core.constants import DEFAULT_BIGWIN_MULTIPLIER
from bot.core.state import BotState
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
from bot.ui.maintenance import run_advanced_menu
from bot.ui.wizard import first_run_wizard

if TYPE_CHECKING:
    from bot.core.config import BotConfig
    from bot.core.db import Database

log = logging.getLogger(__name__)

__all__ = ["first_run_wizard", "run_config_menu"]


async def _toggle_or_edit(
    title: str,
    enabled: bool,
    param_label: str,
    param_display: str,
) -> str:
    """智能子選單:T = 切換 啟用/停用、P = 改參數、Enter = 取消。

    讓使用者明確選擇,避免「只想改參數卻不小心關掉通知」的踩雷。
    回傳 'T' / 'P' / ''(取消)。
    """
    state_str = "✓ 啟用" if enabled else "✗ 停用"
    print(f"\n  📌 {title} = {state_str},{param_label} = {param_display}")
    print("    [T] 切換 啟用/停用")
    print(f"    [P] 修改 {param_label}")
    print("    [Enter] 不變更")
    return (await ainput("  選擇: ")).strip().upper()


# ── 子選單:賭博 ─────────────────────────────────────────────────────
async def _sub_menu_gambling(config: BotConfig) -> None:
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
async def _sub_menu_goals(config: BotConfig, state: BotState) -> None:
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
async def _sub_menu_email(config: BotConfig) -> None:
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
            print(f"  ✓ 達標通知 → {'啟用' if e.notify_goal else '停用'}")
            await wait_enter()
        elif choice == "6":
            e.notify_loss = not e.notify_loss
            print(f"  ✓ 停損通知 → {'啟用' if e.notify_loss else '停用'}")
            await wait_enter()
        elif choice == "7":
            sub = await _toggle_or_edit(
                "中大獎通知", e.notify_bigwin, "賠率門檻", f"{bw_mul:.1f}x",
            )
            if sub == "T":
                e.notify_bigwin = not e.notify_bigwin
                print(f"  ✓ 中大獎通知 → {'啟用' if e.notify_bigwin else '停用'}")
            elif sub == "P":
                v = await ask_float("賠率門檻 (>=幾倍才寄信)", bw_mul, min_val=1.0)
                if v is not None: g.bigwin_multiplier = v
            await wait_enter()
        elif choice == "8":
            sub = await _toggle_or_edit(
                "停擺通知", e.notify_dead, "失敗門檻", str(e.dead_threshold),
            )
            if sub == "T":
                e.notify_dead = not e.notify_dead
                print(f"  ✓ 停擺通知 → {'啟用' if e.notify_dead else '停用'}")
            elif sub == "P":
                v = await ask_int("連續失敗幾次算停擺", e.dead_threshold, min_val=1)
                if v is not None: e.dead_threshold = v
            await wait_enter()
        elif choice == "9":
            e.notify_neko = not e.notify_neko
            print(f"  ✓ 貓娘完成通知 → {'啟用' if e.notify_neko else '停用'}")
            await wait_enter()
        elif choice == "A":
            sub = await _toggle_or_edit(
                "每日摘要", e.notify_digest, "時段", f"{e.digest_hour:02d}:00",
            )
            if sub == "T":
                e.notify_digest = not e.notify_digest
                print(f"  ✓ 每日摘要 → {'啟用' if e.notify_digest else '停用'}")
            elif sub == "P":
                v = await ask_int("摘要時段(0~23 整點)", e.digest_hour, min_val=0, max_val=23)
                if v is not None: e.digest_hour = v
            await wait_enter()


# ── 子選單:貓娘 ────────────────────────────────────────────────────
async def _sub_menu_neko(config: BotConfig) -> None:
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
async def _sub_menu_transfer(config: BotConfig) -> None:
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
async def _sub_menu_dashboard(config: BotConfig) -> None:
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


# ── 子選單:進階下注策略(hourly / rolling / trailing) ─────────────
async def _sub_menu_strategies(config: BotConfig, state: BotState) -> None:
    g = config.gambling
    while True:
        os.system("cls")
        print(f"\n{'═'*52}\n  🎯 進階下注策略\n{'═'*52}")
        print()
        print("  ⓘ 提示:這些策略不會把負 EV 變成正,只能降低 variance / drawdown。")
        print("  ⓘ 進主程式後 Dashboard → 「策略 backtest」看歷史模擬結果")
        print()

        print("  [1] 時段過濾  hourly_filter  -- 跳過歷史 EV/勝率差的小時")
        print(f"      啟用:        {'✓' if g.hourly_filter_enabled else '✗'}")
        print(f"      樣本門檻:    {g.hourly_min_bets}  (該小時 < N 筆 → 不過濾)")
        print(f"      勝率下限:    {g.hourly_min_winrate:.1%}  (低於這個 → 跳)")
        print(f"      EV 下限:     {g.hourly_min_ev:.4f}  (低於這個 → 跳)")
        print()

        print("  [2] 滾動 EV   rolling_window -- 近期 EV 差時減碼/好時加碼")
        print(f"      啟用:        {'✓' if g.rolling_enabled else '✗'}")
        print(f"      視窗筆數:    {g.rolling_window_size}")
        print(f"      減碼門檻 EV: {g.rolling_low_ev:.4f}  → 倍率 {g.rolling_low_mult:.2f}x")
        print(f"      加碼門檻 EV: {g.rolling_high_ev:.4f}  → 倍率 {g.rolling_high_mult:.2f}x")
        print()

        print("  [3] Trailing stop -- 累計淨收從峰值跌幅 > X% → 暫停 N 分鐘")
        print(f"      啟用:        {'✓' if g.trailing_stop_enabled else '✗'}")
        print(f"      跌幅門檻:    {g.trailing_stop_pct:.1f}%")
        print(f"      冷卻分鐘:    {g.trailing_stop_cooldown_min:.0f}")
        print()

        print("  [Runtime 統計]")
        print(f"      hourly 跳過:     {state.strategy_skipped_hourly}")
        print(f"      trailing 跳過:   {state.strategy_skipped_trailing}")
        print(f"      trailing 觸發:   {state.strategy_trailing_triggers} 次")
        print(f"      最近 rolling 倍率: {state.strategy_recent_ev_mult:.2f}x")
        print()

        print("  [0] 返回主選單")
        choice = (await ainput("\n  選擇: ")).strip()
        if choice == "0":
            return
        elif choice == "1":
            await _edit_hourly_filter(g)
        elif choice == "2":
            await _edit_rolling(g)
        elif choice == "3":
            await _edit_trailing(g)


async def _edit_hourly_filter(g) -> None:
    while True:
        os.system("cls")
        print(f"\n{'═'*48}\n  ⏰ 時段過濾\n{'═'*48}")
        print(f"   [1] 啟用 / 停用:     {'✓ 啟用' if g.hourly_filter_enabled else '✗ 停用'}")
        print(f"   [2] 樣本門檻:        {g.hourly_min_bets}  筆")
        print(f"   [3] 勝率下限:        {g.hourly_min_winrate:.2%}")
        print(f"   [4] EV 下限:         {g.hourly_min_ev:.4f}")
        print()
        print("   [0] 返回")
        c = (await ainput("\n  選擇: ")).strip()
        if c == "0":
            return
        elif c == "1":
            g.hourly_filter_enabled = not g.hourly_filter_enabled
            print(f"  ✓ → {'啟用' if g.hourly_filter_enabled else '停用'}")
            await wait_enter()
        elif c == "2":
            v = await ask_int("樣本門檻", g.hourly_min_bets, min_val=1, max_val=10000)
            if v is not None: g.hourly_min_bets = v
            await wait_enter()
        elif c == "3":
            v = await ask_float("勝率下限 (0~1, 例 0.30)", g.hourly_min_winrate,
                                min_val=0.0, max_val=1.0)
            if v is not None: g.hourly_min_winrate = v
            await wait_enter()
        elif c == "4":
            v = await ask_float("EV 下限 (例 0.95)", g.hourly_min_ev,
                                min_val=0.0, max_val=10.0)
            if v is not None: g.hourly_min_ev = v
            await wait_enter()


async def _edit_rolling(g) -> None:
    while True:
        os.system("cls")
        print(f"\n{'═'*48}\n  📊 滾動 EV 動態下注\n{'═'*48}")
        print(f"   [1] 啟用 / 停用:     {'✓ 啟用' if g.rolling_enabled else '✗ 停用'}")
        print(f"   [2] 視窗筆數:        {g.rolling_window_size}")
        print(f"   [3] 減碼 EV 門檻:    {g.rolling_low_ev:.4f}")
        print(f"   [4] 減碼倍率:        {g.rolling_low_mult:.2f}x")
        print(f"   [5] 加碼 EV 門檻:    {g.rolling_high_ev:.4f}")
        print(f"   [6] 加碼倍率:        {g.rolling_high_mult:.2f}x")
        print()
        print("   [0] 返回")
        c = (await ainput("\n  選擇: ")).strip()
        if c == "0":
            return
        elif c == "1":
            g.rolling_enabled = not g.rolling_enabled
            print(f"  ✓ → {'啟用' if g.rolling_enabled else '停用'}")
            await wait_enter()
        elif c == "2":
            v = await ask_int("視窗筆數", g.rolling_window_size,
                              min_val=10, max_val=100000)
            if v is not None: g.rolling_window_size = v
            await wait_enter()
        elif c == "3":
            v = await ask_float("減碼 EV 門檻", g.rolling_low_ev,
                                min_val=0.0, max_val=10.0)
            if v is not None: g.rolling_low_ev = v
            await wait_enter()
        elif c == "4":
            v = await ask_float("減碼倍率 (0~5)", g.rolling_low_mult,
                                min_val=0.0, max_val=5.0)
            if v is not None: g.rolling_low_mult = v
            await wait_enter()
        elif c == "5":
            v = await ask_float("加碼 EV 門檻", g.rolling_high_ev,
                                min_val=0.0, max_val=10.0)
            if v is not None: g.rolling_high_ev = v
            await wait_enter()
        elif c == "6":
            v = await ask_float("加碼倍率 (0~5)", g.rolling_high_mult,
                                min_val=0.0, max_val=5.0)
            if v is not None: g.rolling_high_mult = v
            await wait_enter()


async def _edit_trailing(g) -> None:
    while True:
        os.system("cls")
        print(f"\n{'═'*48}\n  ⛔ Trailing Stop\n{'═'*48}")
        print(f"   [1] 啟用 / 停用:     {'✓ 啟用' if g.trailing_stop_enabled else '✗ 停用'}")
        print(f"   [2] 跌幅門檻 %:      {g.trailing_stop_pct:.1f}")
        print(f"   [3] 冷卻分鐘:        {g.trailing_stop_cooldown_min:.0f}")
        print()
        print("  ⓘ 觸發後暫停 N 分鐘,結束時 baseline 會 reset 到當下,")
        print("    避免「peak 還是過去歷史最高」造成立刻又觸發。")
        print()
        print("   [0] 返回")
        c = (await ainput("\n  選擇: ")).strip()
        if c == "0":
            return
        elif c == "1":
            g.trailing_stop_enabled = not g.trailing_stop_enabled
            print(f"  ✓ → {'啟用' if g.trailing_stop_enabled else '停用'}")
            await wait_enter()
        elif c == "2":
            v = await ask_float("跌幅門檻 %", g.trailing_stop_pct,
                                min_val=0.1, max_val=100.0)
            if v is not None: g.trailing_stop_pct = v
            await wait_enter()
        elif c == "3":
            v = await ask_float("冷卻分鐘", g.trailing_stop_cooldown_min,
                                min_val=1.0, max_val=10080.0)
            if v is not None: g.trailing_stop_cooldown_min = v
            await wait_enter()


# ── 子選單:股票 ──────────────────────────────────────────────────────
async def _sub_menu_stock(config: BotConfig, state: BotState) -> None:
    s = config.stock
    while True:
        os.system("cls")
        print(f"\n{'═'*52}\n  📈 股票監視 / 建議\n{'═'*52}")
        print()
        print("  ⓘ Phase 1-2:純建議,bot 不會自動下單。")
        print("  ⓘ 若 parser 抓不到價格,開 [B] log_raw_text 看實際 embed 格式。")
        print()

        snap = state.stock_last_snapshot or {}
        print("  [目前狀態]")
        print(f"    啟用:        {'✓' if s.enabled else '✗'}")
        if snap:
            ts = snap.get("ts", "─")
            n_prices = len(snap.get("prices", {}))
            n_holds  = len(snap.get("holdings", {}))
            print(f"    最近 poll:   {ts}  (價格 {n_prices} 支,持股 {n_holds} 支)")
        else:
            print("    最近 poll:   尚未 poll(loop 啟動 60 秒後第一次 poll)")
        print()

        print("  [基本設定]")
        print(f"   [1] 啟用 / 停用:    {'✓ 啟用' if s.enabled else '✗ 停用'}")
        print(f"   [2] poll 間隔分鐘:  {s.poll_interval_min}")
        print(f"   [3] 查價指令:       {s.list_command} {s.list_param}".rstrip())
        print(f"   [4] 查持股指令:     {s.portfolio_command} {s.portfolio_param}".rstrip())
        print()
        print("  [分析參數]")
        print(f"   [5] 短均線(MA):    {s.ma_short}")
        print(f"   [6] 長均線(MA):    {s.ma_long}")
        print(f"   [7] 獲利了結 %:     {s.take_profit_pct}")
        print(f"   [8] 停損 %:         {s.stop_loss_pct}")
        print(f"   [9] 強訊號分數門檻: {s.signal_score_threshold}")
        print()
        print("  [Debug / 進階]")
        print(f"   [A] log raw text:   {'✓' if s.log_raw_text else '✗'}  (parser 抓不到時開這個看原文)")
        print(f"   [B] custom regex:   {s.custom_price_pattern or '(空)'}")
        print()
        print("   [0] 返回主選單")
        choice = (await ainput("\n  選擇: ")).strip().upper()
        if choice == "0":
            return
        elif choice == "1":
            s.enabled = not s.enabled
            print(f"  ✓ → {'啟用' if s.enabled else '停用'}")
            await wait_enter()
        elif choice == "2":
            v = await ask_float("poll 間隔分鐘", s.poll_interval_min,
                                min_val=1.0, max_val=1440.0)
            if v is not None: s.poll_interval_min = v
            await wait_enter()
        elif choice == "3":
            v = await ask_text("查價指令(如 /stock 或 /stocks)",
                               s.list_command, max_len=50, allow_chinese=False)
            if v is not None: s.list_command = v
            p = await ask_text("查價指令參數(可空)",
                               s.list_param, max_len=100, allow_empty=True)
            if p is not None: s.list_param = p
            await wait_enter()
        elif choice == "4":
            v = await ask_text("查持股指令(如 /portfolio)",
                               s.portfolio_command, max_len=50, allow_chinese=False)
            if v is not None: s.portfolio_command = v
            p = await ask_text("查持股指令參數(可空)",
                               s.portfolio_param, max_len=100, allow_empty=True)
            if p is not None: s.portfolio_param = p
            await wait_enter()
        elif choice == "5":
            v = await ask_int("短均線 N", s.ma_short, min_val=2, max_val=200)
            if v is not None: s.ma_short = v
            await wait_enter()
        elif choice == "6":
            v = await ask_int("長均線 N", s.ma_long, min_val=5, max_val=500)
            if v is not None: s.ma_long = v
            await wait_enter()
        elif choice == "7":
            v = await ask_float("獲利了結 %", s.take_profit_pct,
                                min_val=0.5, max_val=1000.0)
            if v is not None: s.take_profit_pct = v
            await wait_enter()
        elif choice == "8":
            v = await ask_float("停損 %", s.stop_loss_pct,
                                min_val=0.5, max_val=100.0)
            if v is not None: s.stop_loss_pct = v
            await wait_enter()
        elif choice == "9":
            v = await ask_int("強訊號分數門檻 (0~100)",
                              s.signal_score_threshold, min_val=0, max_val=100)
            if v is not None: s.signal_score_threshold = v
            await wait_enter()
        elif choice == "A":
            s.log_raw_text = not s.log_raw_text
            print(f"  ✓ log_raw_text → {'開' if s.log_raw_text else '關'}")
            await wait_enter()
        elif choice == "B":
            print("  custom regex:必須有 2 個 group(symbol, price);留空用內建。")
            print("  範例: \\b([A-Z]{1,6})\\s+\\$([0-9.]+)")
            v = await ask_text("custom regex(留空清掉)",
                               s.custom_price_pattern, max_len=200,
                               allow_chinese=False, allow_empty=True)
            if v is not None: s.custom_price_pattern = v
            await wait_enter()


# ── 子選單:版本更新 ──────────────────────────────────────────────────
async def _sub_menu_updater(config: BotConfig, state: BotState) -> None:
    u = config.updater
    while True:
        os.system("cls")
        print(f"\n{'═'*48}\n  🔄 版本檢查 / 自動更新\n{'═'*48}")

        local_short  = (state.local_commit  or "")[:7] or "─"
        remote_short = (state.remote_commit or "")[:7] or "─"
        if state.last_update_check:
            from datetime import datetime
            last_str = datetime.fromtimestamp(state.last_update_check).strftime("%H:%M:%S")
        else:
            last_str = "尚未檢查"

        print("  [目前狀態]")
        print(f"   本地版本:  {local_short}")
        print(f"   遠端版本:  {remote_short}")
        if state.update_available:
            print("   狀態:      🔔 [有新版可用,選 [4] 立即更新]")
        elif state.last_update_check:
            print("   狀態:      ✓ 已是最新版")
        else:
            print("   狀態:      尚未檢查(啟動後 30 秒會自動跑第一次)")
        print(f"   上次檢查:  {last_str}")
        print()

        print("  [設定]")
        print(f"   [1] 自動檢查:      {'✓ 啟用' if u.auto_check else '✗ 停用'}")
        print(f"   [2] 檢查間距:      {u.check_interval_min} 分鐘")
        print(f"   [3] 自動更新:      {'✓ 啟用' if u.auto_update else '✗ 停用'}  "
              f"(偵測新版 → git pull + 重啟)")
        print()
        print("  [動作]")
        print("   [4] 立即檢查 / 更新")
        print()
        print("   [0] 返回主選單")
        choice = (await ainput("\n  選擇: ")).strip()
        if choice == "0":
            return
        elif choice == "1":
            u.auto_check = not u.auto_check
            print(f"  ✓ 自動檢查 → {'啟用' if u.auto_check else '停用'}")
            await wait_enter()
        elif choice == "2":
            v = await ask_int("檢查間距分鐘 (建議 30~360)",
                              u.check_interval_min, min_val=5, max_val=1440)
            if v is not None: u.check_interval_min = v
            await wait_enter()
        elif choice == "3":
            u.auto_update = not u.auto_update
            print(f"  ✓ 自動更新 → {'啟用' if u.auto_update else '停用'}")
            if u.auto_update:
                print("  ⚠ 啟用後若偵測到新版會自動 git pull + 重啟,你的本地未提交修改可能會中斷流程")
            await wait_enter()
        elif choice == "4":
            from bot.core.updater import check_for_updates, perform_update
            print("\n  正在檢查 GitHub...")
            status = await check_for_updates(u.branch)
            if status.error:
                print(f"  ⚠ 檢查失敗: {status.error}")
                await wait_enter()
                continue
            local_s  = (status.local_commit  or "")[:7]
            remote_s = (status.remote_commit or "")[:7]
            async with state.lock:
                state.local_commit = status.local_commit
                state.remote_commit = status.remote_commit
                state.update_available = status.has_update
                state.last_update_check = __import__("time").time()
            if not status.has_update:
                print(f"  ✓ 已是最新版({local_s})")
                await wait_enter()
                continue
            print(f"  🔔 有新版: {local_s} → {remote_s}")
            confirm = await ask_yes_no("立即執行 git pull + 重啟程式?")
            if not confirm:
                continue
            print("  正在 git pull...")
            ok, output = await perform_update(u.branch)
            print("  --- git output ---")
            for line in output.strip().splitlines()[-10:]:
                print(f"  {line}")
            print("  ------------------")
            if ok:
                print("  ✓ 更新成功!3 秒後重啟程式...")
                import asyncio
                await asyncio.sleep(3)
                async with state.lock:
                    state.reboot = True
                    state.quit = True
                return
            print("  ⚠ 更新失敗")
            await wait_enter()


# ── 主選單 ────────────────────────────────────────────────────────────
async def run_config_menu(
    config: BotConfig,
    state: BotState,
    db: Database,
    on_save: Callable[[BotConfig], Awaitable[None]],
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
        print("   [2] 🏁 目標 / 停損 / 連敗冷靜")
        print(f"   [3] 📧 Email / 通知 ({'啟用' if config.email.enabled else '停用'})")
        print(f"   [4] 🐱 貓娘監控     ({'啟用' if config.nekomusume.enabled else '停用'}, "
              f"自動領取={'✓' if config.nekomusume.auto_claim else '✗'})")
        print(f"   [5] 💸 自動轉帳     ({'啟用' if config.transfer.enabled else '停用'})")
        print(f"   [6] 🌐 Dashboard    ({'啟用' if config.dashboard.enabled else '停用'}, "
              f"密碼={'已設' if config.dashboard.password else '未設'})")
        gs = config.gambling
        active_strats = []
        if gs.hourly_filter_enabled: active_strats.append("hourly")
        if gs.rolling_enabled:        active_strats.append("rolling")
        if gs.trailing_stop_enabled:  active_strats.append("trailing")
        st_summary = ", ".join(active_strats) if active_strats else "無"
        print(f"   [7] 🎯 進階下注策略 (啟用: {st_summary})")
        st_cfg = config.stock
        st_n_held = len((state.stock_last_snapshot or {}).get("holdings", {}))
        stock_summary = ('啟用' if st_cfg.enabled else '停用')
        if st_cfg.enabled and st_n_held > 0:
            stock_summary += f", 持股 {st_n_held} 支"
        print(f"   [8] 📈 股票監視     ({stock_summary})")
        u = config.updater
        upd_summary = (f"自動檢查={'✓' if u.auto_check else '✗'}, "
                       f"自動更新={'✓' if u.auto_update else '✗'}")
        if state.update_available:
            upd_summary += "  🔔 有新版"
        print(f"   [9] 🔄 版本更新     ({upd_summary})")
        print("   [A] 🛠️  進階(檔案管理 / 系統更新)")
        print()
        print(f"   📊 Slot 分析:       {sa_spins:,} 筆紀錄")
        print()
        print("   [0] 儲存並返回")
        print()

        choice = (await ainput("  選擇: ")).strip().upper()
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
            await _sub_menu_strategies(config, state)
        elif choice == "8":
            await _sub_menu_stock(config, state)
        elif choice == "9":
            await _sub_menu_updater(config, state)
            if state.quit:
                break
        elif choice == "A":
            await run_advanced_menu(state, db)
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

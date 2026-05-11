"""每日摘要 email 內文 + 通知判斷。

把原本散在 main.py 的 _build_digest_body / _maybe_notify_goal /
_maybe_handle_stop_loss / _notify_bigwin / _notify_dead 集中。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING

from bot.core.constants import (
    DEFAULT_GOAL_ACTION,
    DEFAULT_LOSS_ACTION,
    DEFAULT_NOTIFY_USER_ID,
)
from bot.core.state import BotState
from bot.notifications.email import send_email
from bot.slot.analysis import compute_slot_stats, format_kelly_display

if TYPE_CHECKING:
    from playwright.async_api import Page

    from bot.core.config import BotConfig

log = logging.getLogger(__name__)


# ── 每日摘要內文 ────────────────────────────────────────────────────
def build_digest_body(state: BotState, sa: dict) -> str:
    ev_counters = state.events
    since = ev_counters.since_ts or state.session_start_ts
    now = time.time()
    hours = max(0.1, (now - since) / 3600)

    bal = state.balance
    bal_str = f"{bal:,}" if isinstance(bal, int) else "未知"
    start = state.start_balance
    if isinstance(start, int) and isinstance(bal, int):
        diff_str = f"{(bal - start):+,}"
    else:
        diff_str = "─"

    # 區間下注紀錄(只看 since 之後)
    history = state.history or []
    period_records = [r for r in history if _record_ts_after(r, since)]
    p_total = len(period_records)
    p_wins  = sum(1 for r in period_records if r.get("change", 0) > 0)
    p_loss  = sum(1 for r in period_records if r.get("change", 0) < 0)
    p_net   = sum(r.get("change", 0) for r in period_records)
    p_wr    = (p_wins / p_total * 100) if p_total else 0.0

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
        f"/hourly 領取:    {ev_counters.hourly_claims}",
        f"/daily 領取:     {ev_counters.daily_claims}",
        f"自動轉帳:        {ev_counters.transfers}",
        f"貓娘完成:        {ev_counters.neko_completes}",
        f"中大獎次數:      {ev_counters.bigwins}",
        f"達標次數:        {ev_counters.goal_hits}",
        f"停損觸發:        {ev_counters.stop_loss_fires}",
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
        lines.append(f"Kelly f*:   {format_kelly_display(sa_stats)}")

    return "\n".join(lines)


def _record_ts_after(record: dict, since_ts: float) -> bool:
    ts_str = record.get("ts", "")
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        return dt.timestamp() >= since_ts
    except (ValueError, TypeError):
        return True


# ── 達標通知 ────────────────────────────────────────────────────────
async def maybe_notify_goal(
    page: "Page", state: BotState, config: "BotConfig",
    on_config_save,
) -> None:
    """達成 gambling.goal 時 → mention + email + 處理 goal_action。

    `on_config_save` 是個 async callable,簽章 `async def(config: BotConfig)`,
    呼叫端負責把 config 存到 DB。
    """
    from bot.discord.client import notify_goal_reached

    gcfg = config.gambling
    goal = int(gcfg.goal or 0)
    if goal <= 0:
        return
    bal = state.balance
    if bal is None:
        return

    if bal >= goal and not state.goal_reached:
        async with state.lock:
            state.goal_reached = True
            state.events.goal_hits += 1
        user_id = (gcfg.notify_user_id or DEFAULT_NOTIFY_USER_ID).strip()
        action  = (gcfg.goal_action or DEFAULT_GOAL_ACTION).lower()
        step    = int(gcfg.goal_step or 0)

        log.info("達成目標 %d(餘額 %d),動作=%s", goal, bal, action)

        # 1. Discord mention
        try:
            await notify_goal_reached(page, bal, goal, user_id)
        except Exception as e:    # noqa: BLE001
            log.warning("Discord mention 失敗: %s", e)

        # 2. Email
        ecfg = config.email
        if ecfg.enabled and ecfg.notify_goal:
            stats = (
                f"目前餘額: {bal:,}\n"
                f"目標餘額: {goal:,}\n"
                f"起始餘額: {state.start_balance}\n"
                f"本次盈虧: {(bal - (state.start_balance or bal)):+,}\n"
                f"總下注: {state.total_bets}(勝 {state.wins} / 負 {state.losses})\n"
                f"後續動作: {action}"
            )
            await send_email(ecfg, f"[Discord Bot] 達成賭博目標 {goal:,}", stats)

        # 3. 處理後續動作
        if action == "raise" and step > 0:
            # 新邏輯:達標後保底門檻 = 當下目標 - 10000(緩衝),目標 += step
            # 概念:贏到 goal 就把 goal 之下 10000 鎖定,用上面 10000 + 之後贏的
            # 繼續往上推。比舊邏輯(threshold = goal,沒緩衝會立刻撞底)友善。
            from bot.core.constants import GOAL_RAISE_THRESHOLD_BUFFER
            new_threshold = max(0, goal - GOAL_RAISE_THRESHOLD_BUFFER)
            new_goal = goal + step
            gcfg.threshold = new_threshold
            gcfg.goal = new_goal
            await on_config_save(config)
            async with state.lock:
                state.goal_reached = False
            log.info("raise 模式:門檻 → %d(目標 - %d)、目標 → %d",
                     new_threshold, GOAL_RAISE_THRESHOLD_BUFFER, new_goal)
        else:
            gcfg.enabled = False
            await on_config_save(config)
            log.info("pause 模式:賭博已停用")

    elif bal < goal and state.goal_reached:
        async with state.lock:
            state.goal_reached = False


# ── 停損 ────────────────────────────────────────────────────────────
async def maybe_handle_stop_loss(
    state: BotState, config: "BotConfig", on_config_save,
) -> bool:
    gcfg = config.gambling
    floor = int(gcfg.loss_floor or 0)
    if floor <= 0:
        return False
    bal = state.balance
    if bal is None:
        return False

    if bal > floor:
        if state.loss_triggered:
            async with state.lock:
                state.loss_triggered = False
            log.info("餘額 %d 已回到停損點 %d 以上,loss_triggered 重置", bal, floor)
        return False

    if state.loss_triggered:
        return True

    async with state.lock:
        state.loss_triggered = True
        state.events.stop_loss_fires += 1
    action = (gcfg.loss_action or DEFAULT_LOSS_ACTION).lower()
    step   = int(gcfg.loss_step or 0)
    log.warning("觸發停損 %d(餘額 %d),動作=%s", floor, bal, action)
    state.queue_log(f"⛔ 觸發停損 {floor:,}(餘額 {bal:,}),動作={action}")

    ecfg = config.email
    if ecfg.enabled and ecfg.notify_loss:
        body = (
            f"目前餘額: {bal:,}\n"
            f"停損點:   {floor:,}\n"
            f"起始餘額: {state.start_balance}\n"
            f"本次盈虧: {(bal - (state.start_balance or bal)):+,}\n"
            f"總下注: {state.total_bets}(勝 {state.wins} / 負 {state.losses})\n"
            f"後續動作: {action}"
        )
        try:
            await send_email(ecfg, f"[Discord Bot] 觸發停損 {floor:,}", body)
        except Exception as e:    # noqa: BLE001
            log.warning("停損 email 寄出失敗: %s", e)

    if action == "lower_threshold" and step > 0:
        new_threshold = max(0, bal - step)
        new_floor     = max(0, floor - step)
        gcfg.threshold  = new_threshold
        gcfg.loss_floor = new_floor
        await on_config_save(config)
        async with state.lock:
            state.loss_triggered = False
        log.info("lower_threshold 模式:門檻 → %d、停損 → %d", new_threshold, new_floor)
        state.queue_log(f"⛔ 階梯下移:門檻={new_threshold:,} 停損={new_floor:,}")
    else:
        gcfg.enabled = False
        await on_config_save(config)
        log.info("pause 模式:賭博已停用")
        state.queue_log("⛔ 賭博已停用")

    return True


# ── 中大獎 / bot 停擺 ───────────────────────────────────────────────
async def notify_bigwin(
    state: BotState, config: "BotConfig",
    bet: int, gross_win: int, multiplier: float,
) -> None:
    """中大獎時寄 email(Discord 不另發,避免洗版)。"""
    log.info("🎰 中大獎!下注 %d → 贏得 %d (%.2fx)", bet, gross_win, multiplier)
    async with state.lock:
        state.events.bigwins += 1

    ecfg = config.email
    if not (ecfg.enabled and ecfg.notify_bigwin):
        return

    bal = state.balance
    bal_str = f"{bal:,}" if isinstance(bal, int) else "未知"
    body = (
        f"🎰 Discord Bot 中大獎通知\n\n"
        f"下注:        {bet:,}\n"
        f"總計贏得:    {gross_win:,}\n"
        f"淨變動:      +{gross_win - bet:,}\n"
        f"賠率:        {multiplier:.2f}x\n"
        f"目前餘額:    {bal_str}\n"
        f"累計勝/敗:   {state.wins} / {state.losses}\n"
        f"賭博淨收:    {state.net_change:+,}"
    )
    await send_email(
        ecfg, f"[Discord Bot] 🎰 中大獎 {multiplier:.1f}x!贏得 {gross_win:,}", body,
    )


async def notify_stock_news(
    state: BotState, config: "BotConfig",   # noqa: ARG001
    new_items: list[dict],
) -> None:
    """新股票新聞 → email。獨立 toggle ecfg.notify_stock_news。

    new_items 來自 db.upsert_news_items 的回傳(去重後真的新加入的)。
    每項含 symbol / date / title。
    """
    ecfg = config.email
    if not (ecfg.enabled and getattr(ecfg, "notify_stock_news", False)):
        return
    if not new_items:
        return

    # 按 symbol 分組顯示
    by_sym: dict[str, list[dict]] = {}
    for it in new_items:
        by_sym.setdefault(it.get("symbol", "?"), []).append(it)

    syms = sorted(by_sym.keys())
    subj_syms = ", ".join(syms[:3]) + ("..." if len(syms) > 3 else "")
    subj = f"[Discord Bot] 📰 股票新聞 {subj_syms}({len(new_items)} 則)"
    lines = ["📰 偵測到新股票新聞", ""]
    for sym in syms:
        lines.append(f"━━ {sym} ━━")
        for it in by_sym[sym]:
            lines.append(f"  ({it.get('date', '?')}) {it.get('title', '')}")
        lines.append("")
    lines.extend([
        "⚠ 純訊息提示,不構成投資建議。完整新聞請進 Discord /stock symbol:X 看。",
    ])
    await send_email(ecfg, subj, "\n".join(lines))


async def notify_stock_volatility(
    state: BotState, config: "BotConfig",   # noqa: ARG001
    symbol: str, info: dict,
) -> None:
    """股票短期波動警示 → email。info 來自 detect_volatility 的回傳。

    呼叫端要負責 anti-spam(用 state.stock_volatility_notified 追蹤 cooldown)。
    重用 ecfg.notify_stock_signal 開關 — volatility 跟 buy/sell 訊號都是「股票
    通知」家族,共用一個 email toggle 比較不亂。
    """
    ecfg = config.email
    if not (ecfg.enabled and getattr(ecfg, "notify_stock_signal", False)):
        return

    direction = info.get("direction", "rise")
    change = info.get("change_pct", 0.0)
    cur = info.get("current", 0.0)
    base = info.get("baseline", 0.0)
    win = info.get("window_min", 0)
    emoji = "📈" if direction == "rise" else "📉"
    label = "暴漲" if direction == "rise" else "暴跌"
    subj = f"[Discord Bot] {emoji} {symbol} {label} {change:+.2f}% ({win:g} min)"
    body = "\n".join([
        f"{emoji} 股票短期{label}警示",
        "",
        f"標的:    {symbol}",
        f"現價:    {cur:.4f}",
        f"基準價:  {base:.4f}({info.get('baseline_ts', '')})",
        f"變動:    {change:+.2f}% / 過去 {win:g} 分鐘",
        "",
        "⚠ 純價格變動警示,不代表買賣建議 — 只是提醒你看一下。",
    ])
    await send_email(ecfg, subj, body)


async def notify_stock_signal(
    state: BotState, config: "BotConfig",
    symbol: str, signal_type: str, eval_dict: dict,
) -> None:
    """強股票訊號 → email。signal_type ∈ {"buy", "sell"}。

    呼叫端要負責 anti-spam(用 state.stock_notified_signals 追蹤)。
    """
    ecfg = config.email
    if not (ecfg.enabled and getattr(ecfg, "notify_stock_signal", False)):
        return

    score = eval_dict.get("score", 0)
    cur   = eval_dict.get("current", 0)
    reason = eval_dict.get("reason", "")
    emoji = "🟢" if signal_type == "buy" else "🔴"
    action_zh = "買進" if signal_type == "buy" else "賣出"
    subj = f"[Discord Bot] {emoji} 強{action_zh}訊號 {symbol} (score={score})"

    body_lines = [
        f"{emoji} 股票{action_zh}建議",
        "",
        f"標的:    {symbol}",
        f"現價:    {cur:.4f}",
        f"分數:    {score} / 100",
        f"訊號類型: {signal_type.upper()}",
        f"理由:    {reason}",
    ]
    if signal_type == "sell":
        avg = eval_dict.get("avg_cost")
        pp  = eval_dict.get("profit_pct")
        if avg is not None:
            body_lines.append(f"均買價:  {avg:.4f}")
        if pp is not None:
            body_lines.append(f"損益 %:   {pp:+.2f}%")
    body_lines.extend([
        "",
        "⚠ 系統建議僅供參考,bot 不會自動執行交易。",
        "  在 Dashboard /stocks 頁手動操作,或進主程式按 T 看完整分析。",
    ])

    await send_email(ecfg, subj, "\n".join(body_lines))


async def notify_dead(
    state: BotState, config: "BotConfig",
    fail_count: int, context: str = "",
) -> None:
    """連續失敗達門檻時,寄一次 email 提醒。
    用 state.dead_notified 確保同一段「死掉」期間只寄一次。
    """
    if state.dead_notified:
        return
    ecfg = config.email
    if not (ecfg.enabled and ecfg.notify_dead):
        return

    async with state.lock:
        state.dead_notified = True

    bal = state.balance
    bal_str = f"{bal:,}" if isinstance(bal, int) else "未知"
    body = (
        f"⚠️ Discord Bot 可能停擺\n\n"
        f"連續失敗次數: {fail_count}\n"
        f"上次成功餘額: {bal_str}\n"
        f"說明: /balance 與 /slot 連續無法解析回應,已嘗試 reload 頻道頁面。\n"
        f"建議檢查:Discord 登入是否過期、目標 bot 是否在線、頻道權限是否正常。\n\n"
        f"{context}".strip()
    )
    await send_email(
        ecfg, f"[Discord Bot] ⚠️ 警告:bot 可能停擺(連 {fail_count} 次失敗)", body,
    )
    log.warning("已寄出 bot 停擺警告 email(連 %d 次失敗)", fail_count)

"""賭博主 loop。

包含:
- calculate_bet():策略 → 下注金額(auto / fixed / kelly)
- gambling_loop():核心 loop,讀餘額 → 計算 → 下注 → 累計分析 → 通知

Kelly 改進:
- 用 sample variance(已在 compute_slot_stats 處理)
- 半 Kelly(/2),caller 還可再除
- 用 95% CI 下界估算 EV(降低樂觀偏差)
- 樣本不足回退 min_bet
"""
from __future__ import annotations

import logging
import random
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

from bot.core.constants import (
    DEFAULT_BIGWIN_MULTIPLIER,
    DEFAULT_DEAD_THRESHOLD,
    DEFAULT_INTERVAL_MAX,
    DEFAULT_INTERVAL_MIN,
    GAMBLE_RECHECK_SEC,
)
from bot.core.state import BotState, interruptible_sleep, wait_while_paused
from bot.discord.client import get_balance, play_slot, recover_page
from bot.notifications.digest import (
    maybe_handle_stop_loss,
    maybe_notify_goal,
    notify_bigwin,
    notify_dead,
)
from bot.slot.analysis import compute_slot_stats, update_slot_analysis
from bot.slot.strategies import (
    realtime_rolling_multiplier,
    realtime_should_pause_trailing,
    realtime_should_skip_hourly,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

    from bot.core.config import BotConfig
    from bot.core.db import Database

log = logging.getLogger(__name__)


# ── 押注策略 ──────────────────────────────────────────────────────────
def calculate_bet(
    balance: int, gcfg, slot_analysis: dict | None = None,
    rolling_multiplier: float = 1.0,
) -> int:
    """根據策略計算下注金額。回傳 0 = 放棄這把。

    `gcfg` 是 GamblingConfig dataclass。
    `rolling_multiplier`:rolling-EV 策略給的調整倍率(預設 1.0)。
    若是 0 → 直接回 0(放棄這把)。
    """
    threshold = gcfg.threshold
    min_bet   = gcfg.min_bet
    max_bet   = gcfg.max_bet
    fraction  = gcfg.bet_fraction
    strategy  = gcfg.strategy

    excess = balance - threshold
    if excess < min_bet:
        return 0

    if strategy == "kelly" and slot_analysis is not None:
        stats = compute_slot_stats(slot_analysis)
        if stats.get("sufficient_data") and stats.get("kelly_fraction", 0) > 0:
            # 半 Kelly + KELLY_MAX_FRACTION cap(後者已在 compute 端處理)
            half_kelly = stats["kelly_fraction"] / 2
            bet = max(min_bet, int(excess * half_kelly))
        else:
            # 樣本不足或 EV 不足 → 退到最小下注
            bet = min_bet
    elif strategy == "fixed":
        bet = min_bet
    else:   # auto
        bet = max(min_bet, int(excess * fraction))

    # Rolling-EV 倍率(在 max_bet cap 之前套用,讓 cap 還是有效)
    if rolling_multiplier != 1.0:
        bet = int(bet * rolling_multiplier)

    if max_bet > 0:
        bet = min(bet, max_bet)
    bet = max(bet, min_bet)
    if bet > excess:
        return 0
    return bet


RECOVER_THRESHOLD = 3   # 連續失敗達此值就觸發頻道 reload


async def gambling_loop(
    page: "Page", state: BotState,
    config_provider: Callable[[], "BotConfig"],
    on_config_save: Callable[["BotConfig"], Awaitable[None]],
    db: "Database",
) -> None:
    """賭博 loop。"""
    fail_count = 0

    async def _check_dead(ctx: str) -> None:
        ecfg = config_provider().email
        thr = int(ecfg.dead_threshold or DEFAULT_DEAD_THRESHOLD)
        if thr > 0 and fail_count >= thr:
            await notify_dead(state, config_provider(), fail_count, context=ctx)

    async def _reset_fail_state() -> None:
        nonlocal fail_count
        fail_count = 0
        if state.dead_notified:
            log.info("餘額讀取已恢復,重置 dead_notified")
        async with state.lock:
            state.dead_notified = False

    while not state.quit:
        await wait_while_paused(state)
        if state.quit:
            break

        gcfg = config_provider().gambling

        if not gcfg.enabled:
            await interruptible_sleep(state, 30)
            continue

        balance = state.balance
        threshold = gcfg.threshold

        # 餘額未知 → 短間隔重試
        if balance is None:
            log.info("餘額未知,30 秒後重新查詢")
            await interruptible_sleep(state, 30)
            new = await get_balance(page)
            if new is not None:
                async with state.lock:
                    state.balance = new
                    if state.start_balance is None:
                        state.start_balance = new
                log.info("已取得餘額: %d 油幣", new)
                await _reset_fail_state()
                await maybe_notify_goal(page, state, config_provider(), on_config_save)
            else:
                fail_count += 1
                await _check_dead(f"初始 /balance 連 {fail_count} 次失敗")
                if fail_count >= RECOVER_THRESHOLD:
                    log.warning("連續 %d 次抓不到餘額 → 觸發頻道 reload", fail_count)
                    await recover_page(page, state, state.guild_id, state.channel_id)
                    re_bal = await get_balance(page)
                    if re_bal is not None:
                        async with state.lock:
                            state.balance = re_bal
                        log.info("reload 後重抓餘額成功: %d", re_bal)
                        await _reset_fail_state()
                    else:
                        fail_count = 0
            continue

        # 連敗冷靜中
        cd_until = state.cooldown_until_ts
        if cd_until is not None:
            remain = cd_until - time.time()
            if remain > 0:
                await interruptible_sleep(state, min(remain, 30))
                continue
            else:
                async with state.lock:
                    state.cooldown_until_ts = None
                    state.current_streak = 0
                state.queue_log("😌 冷靜結束,繼續下注")

        # 停損檢查
        if await maybe_handle_stop_loss(state, config_provider(), on_config_save):
            await interruptible_sleep(state, 30)
            continue

        if balance <= threshold:
            log.info("餘額 %d ≤ %d,等待 %d 分鐘後重查",
                     balance, threshold, GAMBLE_RECHECK_SEC // 60)
            await interruptible_sleep(state, GAMBLE_RECHECK_SEC)
            new = await get_balance(page)
            if new is not None:
                async with state.lock:
                    state.balance = new
                await _reset_fail_state()
                await maybe_notify_goal(page, state, config_provider(), on_config_save)
            continue

        # ── 進階策略檢查(全部 opt-in) ────────────────────────────
        # (a) Trailing stop 冷卻檢查
        cd_until = state.trailing_cooldown_until_ts
        if cd_until is not None:
            remaining = cd_until - time.time()
            if remaining > 0:
                async with state.lock:
                    state.strategy_skipped_trailing += 1
                await interruptible_sleep(state, min(remaining, 30))
                continue
            # 冷卻結束 → 重設 baseline 到當下 history 長度,
            # 之後的 drawdown 重新從 0 起算(避免立刻又觸發死循環)
            async with state.lock:
                state.trailing_cooldown_until_ts = None
                state.trailing_baseline_idx = len(state.history)
            state.queue_log("⏯ trailing-stop 冷卻結束,baseline reset → resume")
            log.info("trailing stop cooldown ended, baseline_idx=%d",
                     state.trailing_baseline_idx)

        if gcfg.trailing_stop_enabled:
            should_pause, info = realtime_should_pause_trailing(
                state.history, gcfg,
                baseline_idx=state.trailing_baseline_idx,
            )
            if should_pause:
                cooldown_min = max(1.0, float(gcfg.trailing_stop_cooldown_min or 30.0))
                async with state.lock:
                    state.trailing_cooldown_until_ts = time.time() + cooldown_min * 60
                    state.strategy_trailing_triggers += 1
                state.queue_log(
                    f"⛔ trailing-stop 觸發(從峰值 {info['peak']:+,} 跌 "
                    f"{info['drawdown_pct']:.1f}% ≥ {info['threshold']:.1f}%),"
                    f"暫停 {cooldown_min:.0f} 分鐘"
                )
                log.warning("trailing stop 觸發 — 暫停 %.0f 分鐘", cooldown_min)
                continue

        # (b) Hourly filter:當前小時歷史 EV/勝率太差就跳過
        if gcfg.hourly_filter_enabled:
            now_hour = datetime.now().hour
            skip_h, reason = realtime_should_skip_hourly(
                state.history, now_hour, gcfg,
            )
            if skip_h:
                async with state.lock:
                    state.strategy_skipped_hourly += 1
                log.info("hourly filter 跳過: %s", reason)
                await interruptible_sleep(state, 60)
                continue

        # (c) Rolling EV:近期 EV 差時減碼、好時加碼
        roll_mult, roll_ev = realtime_rolling_multiplier(state.history, gcfg)
        if roll_mult != 1.0:
            async with state.lock:
                state.strategy_recent_ev_mult = roll_mult
            ev_str = f"EV={roll_ev:.4f}" if roll_ev is not None else "EV=─"
            log.info("rolling-EV: %s → 倍率 %.2fx", ev_str, roll_mult)
        else:
            async with state.lock:
                state.strategy_recent_ev_mult = 1.0

        bet = calculate_bet(balance, gcfg, state.slot_analysis,
                            rolling_multiplier=roll_mult)
        if bet <= 0:
            await interruptible_sleep(state, 30)
            continue

        async with state.lock:
            state.current_bet = bet
        log.info("餘額 %d > %d,下注 %d", balance, threshold, bet)

        result = await play_slot(page, bet)

        if result is not None:
            await _process_slot_result(
                state, db, config_provider(), bet, result, on_config_save,
            )
            await _reset_fail_state()
            await maybe_notify_goal(page, state, config_provider(), on_config_save)
        else:
            fail_count += 1
            log.warning("無法解析餘額(連續第 %d 次失敗)", fail_count)
            await _check_dead(f"/slot 連 {fail_count} 次失敗")

            if fail_count == 2:
                log.info("試 /balance 重新對齊餘額")
                new = await get_balance(page)
                if new is not None:
                    async with state.lock:
                        state.balance = new
                    log.info("/balance 取得餘額: %d", new)
                    await _reset_fail_state()
            elif fail_count >= RECOVER_THRESHOLD:
                log.warning("連續 %d 次失敗 → 觸發頻道 reload", fail_count)
                ok = await recover_page(page, state, state.guild_id, state.channel_id)
                if ok:
                    re_bal = await get_balance(page)
                    if re_bal is not None:
                        async with state.lock:
                            state.balance = re_bal
                        log.info("reload 後重抓餘額成功: %d", re_bal)
                        await _reset_fail_state()
                    else:
                        fail_count = 0
                else:
                    fail_count = 0

        i_min = float(gcfg.interval_min or DEFAULT_INTERVAL_MIN)
        i_max = float(gcfg.interval_max or DEFAULT_INTERVAL_MAX)
        if i_max < i_min:
            i_max = i_min
        await interruptible_sleep(state, random.uniform(i_min, i_max))


async def _process_slot_result(
    state: BotState, db: "Database", config: "BotConfig",
    bet: int, result: dict,
    on_config_save: Callable[["BotConfig"], Awaitable[None]],   # noqa: ARG001
) -> None:
    """處理一次 slot 結果:更新 state、累計分析、寫 history、中大獎通知。

    所有對 state 的「讀-改-寫」都在 state.lock 區段內;DB I/O 走 async。
    """
    new_balance   = result["balance"]
    parsed_change = result["change"]
    grid_conf     = result.get("grid_confidence", 1.0)

    # 計算 change(優先用 slot embed 解析的;沒有再退到 餘額差)
    if parsed_change is not None:
        change = parsed_change
    else:
        # state.balance 可能在這幾秒被 hourly/daily 修改;但這是邊界 case
        change = new_balance - (state.balance or new_balance)
        log.warning("slot embed 無法解析勝負,用餘額差分(可能因 hourly 干擾不準)")

    history_record = {
        "ts":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "bet":    bet,
        "before": new_balance - change,
        "after":  new_balance,
        "change": change,
        "result": "win" if change > 0 else "loss",
        "lines":  result.get("lines", []),
    }

    # ── 在 state.lock 內做所有 in-memory 修改 ──────────────────────
    bigwin_info = None
    async with state.lock:
        state.net_change += change
        state.total_bets += 1
        if change > 0:
            state.wins += 1
            state.current_streak = (
                state.current_streak + 1 if state.current_streak > 0 else 1
            )
            if state.current_streak > state.max_win_streak:
                state.max_win_streak = state.current_streak
        else:
            state.losses += 1
            state.current_streak = (
                state.current_streak - 1 if state.current_streak < 0 else -1
            )
            if abs(state.current_streak) > state.max_loss_streak:
                state.max_loss_streak = abs(state.current_streak)

            # 連敗冷靜
            pause_n = int(config.gambling.loss_streak_pause or 0)
            if pause_n > 0 and abs(state.current_streak) >= pause_n:
                cooldown_min = float(config.gambling.loss_streak_cooldown_min or 0)
                if cooldown_min > 0:
                    state.cooldown_until_ts = time.time() + cooldown_min * 60
                    state.queue_log(
                        f"😤 連敗 {abs(state.current_streak)} 場 → "
                        f"冷靜 {cooldown_min:.0f} 分鐘"
                    )
                    log.warning(
                        "連敗 %d 場 ≥ %d,暫停下注 %.1f 分鐘",
                        abs(state.current_streak), pause_n, cooldown_min,
                    )

        state.balance = new_balance
        state.history.append(history_record)
        # 只保留最近 N 筆在記憶體鏡像(DB 那邊也會自己 trim)
        from bot.core.constants import HISTORY_MAX_LEN
        if len(state.history) > HISTORY_MAX_LEN:
            state.history = state.history[-HISTORY_MAX_LEN:]

        # 累加 slot 分析
        update_slot_analysis(
            state.slot_analysis, bet, change,
            result.get("lines", []), result.get("grid"),
            grid_confidence=grid_conf,
        )
        spins = state.slot_analysis.get("total_spins", 0)
        # 判斷是否中大獎(計算結束後再通知,避免 lock 內等 SMTP)
        if parsed_change is not None and change > 0 and bet > 0:
            gross_win = change + bet
            multiplier = gross_win / bet
            bigwin_threshold = float(
                config.gambling.bigwin_multiplier or DEFAULT_BIGWIN_MULTIPLIER
            )
            if bigwin_threshold > 0 and multiplier >= bigwin_threshold:
                bigwin_info = (bet, gross_win, multiplier)

    log.info("結果: %s%d | 餘額: %d | 勝/敗: %d/%d",
             "+" if change >= 0 else "", change, new_balance,
             state.wins, state.losses)

    # ── DB I/O(在 lock 外) ───────────────────────────────────────
    try:
        await db.append_history(history_record)
    except Exception:    # noqa: BLE001
        log.exception("寫入 history 失敗")

    # 每 20 spin 存一次 slot_analysis(快照,即使中途 crash 也不會丟太多)
    if spins % 20 == 0:
        try:
            await db.save_slot_analysis(state.slot_analysis)
        except Exception:    # noqa: BLE001
            log.exception("寫入 slot_analysis 失敗")

    # ── 中大獎通知(可能涉及 SMTP,放最後) ───────────────────────
    if bigwin_info is not None:
        bet_w, gross_win, multiplier = bigwin_info
        await notify_bigwin(state, config, bet_w, gross_win, multiplier)

"""下注策略 — hourly filter / rolling EV / trailing stop。

設計原則:
- 三個策略獨立、可組合(各自一個 enable flag)
- 即時決策函式純函式:輸入歷史 + 當下狀態,輸出決策
- Backtest 在歷史 history 上 replay,O(N) 複雜度

數學現實:slot 的 EV 是負的、long-run 任何策略都不會把 -EV 變成 +EV。
這些策略的目的是 risk management:
- Hourly filter:避開歷史最爛時段(降低 variance)
- Rolling EV:近期 EV 差時減碼、好時加碼(降低 max drawdown)
- Trailing stop:從峰值跌幅超過 X% → 暫停,避免單次連敗摧毀帳戶
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)


# ── 共用工具 ──────────────────────────────────────────────────────────
def _parse_hour(ts: str) -> int | None:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").hour
    except (ValueError, TypeError):
        return None


def _parse_dt(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


# ── 1. Hourly filter ─────────────────────────────────────────────────
def hourly_stats(history: list[dict]) -> dict[int, dict]:
    """歷史依「小時 of day」拆成 24 桶,每桶算 bets / wins / EV。

    EV = 總賠付 / 總下注 = sum(bet+change) / sum(bet)。
    """
    buckets: list[dict] = [
        {"hour": h, "bets": 0, "wins": 0, "wagered": 0, "won": 0}
        for h in range(24)
    ]
    for r in history:
        h = _parse_hour(r.get("ts", ""))
        if h is None:
            continue
        bet = r.get("bet", 0) or 0
        change = r.get("change", 0) or 0
        b = buckets[h]
        b["bets"] += 1
        if change > 0:
            b["wins"] += 1
        b["wagered"] += bet
        b["won"] += bet + change

    out = {}
    for b in buckets:
        ev = b["won"] / b["wagered"] if b["wagered"] > 0 else 0.0
        wr = b["wins"] / b["bets"] if b["bets"] > 0 else 0.0
        out[b["hour"]] = {**b, "ev": ev, "win_rate": wr}
    return out


def hourly_should_skip(
    h_stats: dict[int, dict],
    hour: int,
    min_bets: int,
    min_winrate: float,
    min_ev: float,
) -> tuple[bool, str]:
    """根據預先算好的 hourly_stats 判斷某小時是否該跳過。

    回傳 (should_skip, reason)。
    """
    s = h_stats.get(hour, {"bets": 0})
    if s["bets"] < min_bets:
        return False, ""    # 樣本不足 → 不過濾
    if s["win_rate"] < min_winrate:
        return True, (f"hourly: {hour:02d}h 勝率 {s['win_rate']:.1%} "
                      f"< {min_winrate:.1%}")
    if s["ev"] < min_ev:
        return True, (f"hourly: {hour:02d}h EV {s['ev']:.4f} "
                      f"< {min_ev:.4f}")
    return False, ""


# ── 2. Rolling-window EV ─────────────────────────────────────────────
def rolling_ev(history: list[dict], window: int) -> float | None:
    """最近 window 筆的 EV(賠付率)。樣本不足回 None。"""
    if window <= 0 or len(history) < window:
        return None
    recent = history[-window:]
    wagered = sum((r.get("bet", 0) or 0) for r in recent)
    if wagered == 0:
        return None
    won = sum(((r.get("bet", 0) or 0) + (r.get("change", 0) or 0)) for r in recent)
    return won / wagered


def rolling_multiplier(
    recent_ev: float | None,
    low_ev: float,
    high_ev: float,
    low_mult: float,
    high_mult: float,
) -> float:
    """根據最近 EV 給下注倍率。樣本不足或落在中段 → 1.0。"""
    if recent_ev is None:
        return 1.0
    if recent_ev < low_ev:
        return low_mult
    if recent_ev > high_ev:
        return high_mult
    return 1.0


# ── 3. Trailing stop ─────────────────────────────────────────────────
def compute_drawdown_pct(history: list[dict]) -> tuple[float, int, int]:
    """從歷史算累計淨收的 peak / current / drawdown%。

    回傳 (drawdown_pct, peak, current)。
    用「累計淨收」為基底而非餘額(避免 hourly/daily/transfer 干擾)。
    drawdown_pct = (peak - current) / max(|peak|, 1) * 100
    """
    if not history:
        return 0.0, 0, 0
    cum = 0
    peak = 0
    for r in history:
        cum += r.get("change", 0) or 0
        peak = max(peak, cum)
    drawdown = peak - cum
    base = max(abs(peak), 1)    # 用 |peak| 處理 peak < 0 的 edge case
    return drawdown / base * 100, peak, cum


# ── Backtest engine ──────────────────────────────────────────────────
def backtest_all(history: list[dict], scfg: dict[str, Any]) -> dict[str, dict]:
    """在 history 上 replay 各策略,比較淨收 / drawdown / 下注次數。

    `scfg` 是策略 config dict(從 GamblingConfig 萃取):
      hourly_filter_enabled, hourly_min_bets, hourly_min_winrate, hourly_min_ev
      rolling_enabled, rolling_window_size, rolling_low_ev, rolling_high_ev,
        rolling_low_mult, rolling_high_mult
      trailing_stop_enabled, trailing_stop_pct, trailing_stop_cooldown_bets

    回傳 dict,key 是策略名,value 是 {net, max_drawdown, n_bets, n_skipped,
        win_rate, peak, end_balance_change}。

    Trailing stop semantics(在 backtest 內):
      觸發後跳過接下來 cooldown_bets 筆,然後 resume + 重設 peak。
      用「下注數」而非「分鐘」是為了 backtest 不依賴 ts 間距。
    """
    if not history:
        return {}

    # 預先算 hourly_stats(用整段 history,跟真實 bot 行為一致 — 真實 bot
    # 每次決策都用「目前累積到當下」的 stats,但 12102 筆下小時分佈早已
    # converge,沒必要在 backtest 內每筆重算)
    h_stats = hourly_stats(history)

    hourly_on = scfg.get("hourly_filter_enabled", False)
    h_min_bets = int(scfg.get("hourly_min_bets", 50))
    h_min_wr = float(scfg.get("hourly_min_winrate", 0.30))
    h_min_ev = float(scfg.get("hourly_min_ev", 0.95))

    rolling_on = scfg.get("rolling_enabled", False)
    r_window = int(scfg.get("rolling_window_size", 500))
    r_low_ev = float(scfg.get("rolling_low_ev", 0.95))
    r_high_ev = float(scfg.get("rolling_high_ev", 1.02))
    r_low_mult = float(scfg.get("rolling_low_mult", 0.5))
    r_high_mult = float(scfg.get("rolling_high_mult", 1.5))

    trailing_on = scfg.get("trailing_stop_enabled", False)
    t_pct = float(scfg.get("trailing_stop_pct", 5.0))
    t_cooldown = int(scfg.get("trailing_stop_cooldown_bets", 100))

    def _replay(use_hourly: bool, use_rolling: bool, use_trailing: bool) -> dict:
        cum = 0
        peak = 0
        max_dd = 0
        n_bets = 0
        n_wins = 0
        n_skipped_hourly = 0
        n_skipped_trailing = 0
        n_trailing_triggers = 0
        # rolling window:用 deque 做 O(1) sliding
        win_bets = deque(maxlen=r_window if use_rolling else 0)
        win_won  = deque(maxlen=r_window if use_rolling else 0)
        cooldown_remaining = 0

        for r in history:
            change = r.get("change", 0) or 0
            bet = r.get("bet", 0) or 0
            hour = _parse_hour(r.get("ts", ""))

            # Trailing stop cooldown
            if use_trailing and cooldown_remaining > 0:
                cooldown_remaining -= 1
                n_skipped_trailing += 1
                if cooldown_remaining == 0:
                    peak = cum    # 重設 peak
                continue

            # Hourly filter
            if use_hourly and hour is not None:
                skip, _ = hourly_should_skip(
                    h_stats, hour, h_min_bets, h_min_wr, h_min_ev,
                )
                if skip:
                    n_skipped_hourly += 1
                    continue

            # Rolling multiplier
            mult = 1.0
            if use_rolling and len(win_bets) >= r_window and sum(win_bets) > 0:
                ev = sum(win_won) / sum(win_bets)
                mult = rolling_multiplier(
                    ev, r_low_ev, r_high_ev, r_low_mult, r_high_mult,
                )

            scaled = int(round(change * mult))
            cum += scaled
            n_bets += 1
            if scaled > 0:
                n_wins += 1
            peak = max(peak, cum)
            dd = peak - cum
            max_dd = max(max_dd, dd)

            # Trailing stop trigger 檢查(在 update peak 之後)
            if use_trailing and peak > 0:
                dd_pct = (peak - cum) / max(abs(peak), 1) * 100
                if dd_pct >= t_pct:
                    cooldown_remaining = t_cooldown
                    n_trailing_triggers += 1

            # 更新 rolling window(scaled bet vs scaled won)
            if use_rolling:
                # 用 scaled 後的數字,讓 rolling EV 反映實際下注規模
                scaled_bet = int(round(bet * mult)) if bet > 0 else 0
                if scaled_bet > 0:
                    win_bets.append(scaled_bet)
                    win_won.append(scaled_bet + scaled)

        return {
            "net":             cum,
            "max_drawdown":    max_dd,
            "n_bets":          n_bets,
            "n_skipped_hourly":   n_skipped_hourly,
            "n_skipped_trailing": n_skipped_trailing,
            "n_trailing_triggers": n_trailing_triggers,
            "win_rate":        (n_wins / n_bets) if n_bets > 0 else 0.0,
            "peak":            peak,
        }

    out: dict[str, dict] = {}
    out["baseline"]      = _replay(False, False, False)
    if hourly_on:
        out["hourly_only"]   = _replay(True,  False, False)
    if rolling_on:
        out["rolling_only"]  = _replay(False, True,  False)
    if trailing_on:
        out["trailing_only"] = _replay(False, False, True)
    if hourly_on or rolling_on or trailing_on:
        out["combined"] = _replay(hourly_on, rolling_on, trailing_on)
    return out


# ── 即時決策包裝(給 gambling_loop 用) ─────────────────────────────
def realtime_should_skip_hourly(
    history: list[dict], now_hour: int, gcfg,
) -> tuple[bool, str]:
    """即時決定當下小時是否該跳過。"""
    if not getattr(gcfg, "hourly_filter_enabled", False):
        return False, ""
    if not history:
        return False, ""
    h_stats = hourly_stats(history)
    return hourly_should_skip(
        h_stats, now_hour,
        int(getattr(gcfg, "hourly_min_bets", 50)),
        float(getattr(gcfg, "hourly_min_winrate", 0.30)),
        float(getattr(gcfg, "hourly_min_ev", 0.95)),
    )


def realtime_rolling_multiplier(history: list[dict], gcfg) -> tuple[float, float | None]:
    """即時算 rolling EV → 倍率。回傳 (multiplier, ev_or_None)。"""
    if not getattr(gcfg, "rolling_enabled", False):
        return 1.0, None
    window = int(getattr(gcfg, "rolling_window_size", 500))
    ev = rolling_ev(history, window)
    mult = rolling_multiplier(
        ev,
        float(getattr(gcfg, "rolling_low_ev", 0.95)),
        float(getattr(gcfg, "rolling_high_ev", 1.02)),
        float(getattr(gcfg, "rolling_low_mult", 0.5)),
        float(getattr(gcfg, "rolling_high_mult", 1.5)),
    )
    return mult, ev


def realtime_should_pause_trailing(history: list[dict], gcfg) -> tuple[bool, dict]:
    """即時檢查 trailing stop。回傳 (should_pause, info_dict)。"""
    if not getattr(gcfg, "trailing_stop_enabled", False):
        return False, {}
    pct, peak, cur = compute_drawdown_pct(history)
    threshold = float(getattr(gcfg, "trailing_stop_pct", 5.0))
    return pct >= threshold, {
        "drawdown_pct": pct, "peak": peak, "current": cur, "threshold": threshold,
    }

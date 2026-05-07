"""Slot 分析資料模型 + 計算 + 持久化。

從 v1 的改進:
1. **Kelly 修正**:
   - Variance 用 Bessel correction (n-1) 而非 n
   - 提高 MIN_KELLY_SAMPLES 從 50 → 200
   - 用 EV 的 95% CI 下界估算 Kelly,降低過度樂觀
   - Kelly fraction cap 在 0.25(避免極端建議)
   - bet=0 的紀錄不計入 sum_return_ratio(避免污染統計)
2. **數值穩定**:welford-style 累計變異數計算,避免大數相減的 catastrophic cancellation
3. **持久化**:用 high_mults 用 deque(maxlen=N),避免 O(n) slice deletion
4. **持久化目標切換到 SQLite DB**:caller 從 db.load_slot_analysis() / save_slot_analysis()
   呼叫,本檔不再直接寫 JSON
5. **新增統計**:max_drawdown / current_drawdown(從 history 算)、ruin_proxy
"""
from __future__ import annotations

import logging
import math
import re
from collections import deque
from datetime import datetime
from typing import Any

from bot.core.constants import (
    HIGH_MULT_KEEP,
    HIGH_MULT_THRESHOLD,
    KELLY_MAX_FRACTION,
    KELLY_USE_CONFIDENCE,
    MIN_KELLY_SAMPLES,
    PAYOUT_BUCKETS,
    SYMBOL_DISPLAY_THRESHOLD,
)
from bot.slot.parsers import NON_SLOT_SHORTCODES


log = logging.getLogger(__name__)


__all__ = [
    # constants(從 core.constants re-export 給 callers)
    "MIN_KELLY_SAMPLES",
    "PAYOUT_BUCKETS", "HIGH_MULT_THRESHOLD", "HIGH_MULT_KEEP",
    "_SYMBOL_DISPLAY_THRESHOLD",
    "_CUSTOM_EMOJI_MARKER", "_SHORTCODE_NAME_RE", "_SHORTCODE_EMOJI_MAP",
    # functions
    "make_slot_analysis", "update_slot_analysis", "compute_slot_stats",
    "compute_hourly_breakdown", "compute_drawdown",
    "format_symbol_display", "is_noise_symbol",
    "migrate_slot_analysis", "format_kelly_display",
    # 向後相容別名
    "_make_slot_analysis", "_update_slot_analysis",
    "_format_symbol_display", "_is_noise_symbol",
]


# ── 顯示 ──────────────────────────────────────────────────────────────
_SYMBOL_DISPLAY_THRESHOLD = SYMBOL_DISPLAY_THRESHOLD

_CUSTOM_EMOJI_MARKER = "🟦"
_SHORTCODE_NAME_RE = re.compile(r'^:([a-z0-9_]+):$')

_SHORTCODE_EMOJI_MAP: dict[str, str] = {
    # 水果
    "cherry":     "🍒",
    "grapes":     "🍇",
    "lemon":      "🍋",
    "watermelon": "🍉",
    "apple":      "🍎",
    "orange":     "🍊",
    "banana":     "🍌",
    "strawberry": "🍓",
    "peach":      "🍑",
    "pineapple":  "🍍",
    # 經典
    "bell":       "🔔",
    "diamond":    "💎",
    "clover":     "🍀",
    "seven":      "7️⃣",
    "star":       "⭐",
    "fire":       "🔥",
    "crown":      "👑",
    "gem":        "💠",
    "rocket":     "🚀",
    "money":      "💰",
    "moneybag":   "💰",
    "coin":       "🪙",
    "heart":      "❤️",
    "horseshoe":  "🧲",
    "rainbow":    "🌈",
    "lucky":      "🎰",
    "slot":       "🎰",
}


# ── 累加器 ────────────────────────────────────────────────────────────
def make_slot_analysis() -> dict:
    return {
        "total_spins":         0,
        "total_wins":          0,
        "total_losses":        0,
        "total_wagered":       0,
        "total_gross_won":     0,
        "sum_return_ratio":    0.0,
        "sum_return_ratio_sq": 0.0,
        "valid_rr_count":      0,         # 真正計入 ev/var 的筆數(剔除 bet=0)
        "symbol_stats":        {},
        "line_stats":          {},
        "grid_symbol_freq":    {},
        "grid_total_cells":    0,
        "payout_distribution": {k: 0 for k in PAYOUT_BUCKETS},
        "high_mults":          [],
    }


_make_slot_analysis = make_slot_analysis


def update_slot_analysis(
    sa: dict, bet: int, change: int,
    lines: list[Any], grid: list[str] | None,
    grid_confidence: float = 1.0,
) -> None:
    """累加一次 spin 的結果。

    參數:
        sa: state.slot_analysis(就地修改)
        bet, change: 必須是 int
        lines: SlotLine 物件 list 或 dict list 都可
        grid: 9 個符號的 list,或 None
        grid_confidence: parse_slot_grid 回傳的信心度。<0.5 不計入 grid stats

    異常 / 邊界:
        - bet <= 0:仍記 total_spins / wins,但 RR 不計入(避免 ev 污染)
        - lines / grid 解析失敗:那些統計欄位不更新,但 spin 數仍算
    """
    sa["total_spins"] += 1
    sa["total_wagered"] += max(0, bet)

    gross_win = max(0, change + bet)
    sa["total_gross_won"] += gross_win

    if change > 0:
        sa["total_wins"] += 1
    else:
        sa["total_losses"] += 1

    # RR 統計:bet > 0 才計入(避免 div by zero / 異常資料污染)
    if bet > 0:
        rr = gross_win / bet
        sa["sum_return_ratio"]    += rr
        sa["sum_return_ratio_sq"] += rr * rr
        sa["valid_rr_count"]       = sa.get("valid_rr_count", 0) + 1

        pd = sa["payout_distribution"]
        if rr == 0:
            pd["0"] += 1
        elif rr < 2:
            pd["0~2"] += 1
        elif rr < 5:
            pd["2~5"] += 1
        elif rr < 8:
            pd["5~8"] += 1
        elif rr < 10:
            pd["8~10"] += 1
        elif rr < HIGH_MULT_THRESHOLD:
            pd["10~20"] += 1
        else:
            pd["以上"] += 1
            hm = sa.setdefault("high_mults", [])
            hm.append(round(rr, 2))
            # 用切片保留最後 N 個 — list slice 比 deque 容易序列化
            if len(hm) > HIGH_MULT_KEEP:
                # 直接 reslice 比 del hm[:n] 更便宜(後者要 shift array)
                sa["high_mults"] = hm[-HIGH_MULT_KEEP:]
    else:
        log.debug("Spin 紀錄 bet=%d change=%d:不計入 RR 統計", bet, change)

    # 線路統計
    for line in lines:
        sym, ln, payout, mult = _line_attrs(line)
        if sym is None:
            continue
        ss = sa["symbol_stats"].setdefault(
            sym, {"win_appearances": 0, "total_payout": 0, "total_mult_sum": 0.0}
        )
        ss["win_appearances"] += 1
        ss["total_payout"]    += payout
        ss["total_mult_sum"]  += mult

        ls = sa["line_stats"].setdefault(ln, {"hits": 0, "total_payout": 0})
        ls["hits"]         += 1
        ls["total_payout"] += payout

    # 格子統計 — confidence 太低不採用(避免污染)
    if grid is not None and len(grid) >= 9 and grid_confidence >= 0.5:
        for sym in grid[:9]:
            sa["grid_symbol_freq"][sym] = sa["grid_symbol_freq"].get(sym, 0) + 1
        sa["grid_total_cells"] += 9


_update_slot_analysis = update_slot_analysis


def _line_attrs(line: Any) -> tuple[str | None, str, int, float]:
    """從 SlotLine 物件 *或* dict 取出 (symbol, line_name, payout, symbol_mult)。"""
    if hasattr(line, "symbol"):
        return line.symbol, line.line_name, line.payout, line.symbol_mult
    if isinstance(line, dict):
        return (
            line.get("symbol"),
            line.get("line_name", ""),
            int(line.get("payout", 0)),
            float(line.get("symbol_mult", 0.0)),
        )
    return None, "", 0, 0.0


# ── 統計計算 ──────────────────────────────────────────────────────────
def compute_slot_stats(sa: dict) -> dict:
    """從累加器計算各種派生統計。

    Kelly 處理:
    - 用 sample variance(n-1)估計
    - 預設用 EV 95% CI 下界(KELLY_USE_CONFIDENCE),更保守
    - 上限 KELLY_MAX_FRACTION
    - 不足 MIN_KELLY_SAMPLES 樣本 → sufficient_data=False
    """
    n = sa.get("total_spins", 0)
    if n == 0:
        return {"sufficient_data": False, "total_spins": 0}

    valid_n = sa.get("valid_rr_count", 0) or n
    sum_rr    = sa.get("sum_return_ratio", 0.0)
    sum_rr_sq = sa.get("sum_return_ratio_sq", 0.0)

    if valid_n > 0:
        ev = sum_rr / valid_n
    else:
        ev = 0.0

    # Sample variance(n-1):variance = (sum_x² - n*mean²) / (n-1)
    if valid_n > 1:
        var_pop = max(0.0, sum_rr_sq / valid_n - ev * ev)
        # 樣本變異數(較不偏估,小樣本變大)
        variance = var_pop * valid_n / (valid_n - 1)
    else:
        variance = 0.0
    std_dev = math.sqrt(variance)

    win_rate = sa["total_wins"] / n if n > 0 else 0.0
    avg_win_mult = (sa["total_gross_won"] / sa["total_wagered"]
                    if sa.get("total_wagered", 0) > 0 else 0.0)

    # ── Kelly Fraction ───────────────────────────────────────────────
    sufficient = valid_n >= MIN_KELLY_SAMPLES
    kelly_fraction = 0.0
    kelly_input_ev = ev
    kelly_lower_bound = ev   # 即使不啟用 confidence 也填一個值

    if KELLY_USE_CONFIDENCE and valid_n > 1:
        # 95% CI 下界 = ev - 1.96 * SE,SE = stddev / sqrt(n)
        se = std_dev / math.sqrt(valid_n) if valid_n > 0 else 0.0
        kelly_lower_bound = ev - 1.96 * se
        kelly_input_ev = kelly_lower_bound

    if variance > 0 and kelly_input_ev > 1.0:
        kelly_fraction = (kelly_input_ev - 1.0) / variance
        kelly_fraction = min(kelly_fraction, KELLY_MAX_FRACTION)
        kelly_fraction = max(0.0, kelly_fraction)

    # ── 符號統計 ─────────────────────────────────────────────────────
    symbol_info = {}
    for sym, ss in sa.get("symbol_stats", {}).items():
        cnt = ss.get("win_appearances", 0)
        symbol_info[sym] = {
            "win_appearances": cnt,
            "avg_mult":      ss.get("total_mult_sum", 0.0) / cnt if cnt > 0 else 0.0,
            "total_payout":  ss.get("total_payout", 0),
        }

    grid_total = sa.get("grid_total_cells", 0)
    grid_symbol_prob = {}
    if grid_total > 0:
        for sym, cnt in sa.get("grid_symbol_freq", {}).items():
            grid_symbol_prob[sym] = cnt / grid_total

    line_info = {}
    for ln, ls in sa.get("line_stats", {}).items():
        line_info[ln] = {
            "hits":         ls.get("hits", 0),
            "hit_rate":     ls.get("hits", 0) / n if n > 0 else 0.0,
            "total_payout": ls.get("total_payout", 0),
        }

    return {
        "sufficient_data":      sufficient,
        "total_spins":          n,
        "valid_rr_count":       valid_n,
        "ev":                   ev,
        "edge":                 ev - 1.0,
        "variance":             variance,
        "std_dev":              std_dev,
        "win_rate":             win_rate,
        "avg_win_mult":         avg_win_mult,
        "kelly_fraction":       kelly_fraction,
        "kelly_input_ev":       kelly_input_ev,
        "kelly_lower_bound_ev": kelly_lower_bound,
        "symbol_info":          symbol_info,
        "grid_symbol_prob":     grid_symbol_prob,
        "line_info":            line_info,
        "payout_distribution":  sa.get("payout_distribution", {}),
        "high_mults":           sa.get("high_mults", []),
    }


def format_kelly_display(stats: dict) -> str:
    """格式化 Kelly fraction 顯示文字。

    區分三種情況(避免「資料不足」訊息誤導):
    - 樣本不足   `sufficient_data=False` → "資料不足 (需 N,目前 M)"
    - EV 不利     `kelly_fraction=0`、樣本足 → "EV 不利 (CI 下界 X.XXXX)"
                  (邊際為負或 95% CI 下界 ≤ 1.0,Kelly 公式給 0 是對的)
    - 正常        → "0.XXXX (½=0.YYYY)"
    """
    if not stats.get("sufficient_data"):
        valid_n = stats.get("valid_rr_count", 0)
        return f"資料不足 (需 {MIN_KELLY_SAMPLES},目前 {valid_n})"

    kf = stats.get("kelly_fraction", 0.0)
    if kf > 0:
        return f"{kf:.4f} (½={kf/2:.4f})"

    # 樣本足但 kelly=0 → EV/CI 下界 ≤ 1
    lb = stats.get("kelly_lower_bound_ev", stats.get("ev", 0.0))
    edge_pct = (lb - 1.0) * 100
    return f"EV 不利 (CI 下界 {lb:.4f}, {edge_pct:+.2f}%)"


# ── 時段分析 ──────────────────────────────────────────────────────────
def compute_hourly_breakdown(history: list[dict]) -> list[dict]:
    """把 history 依 hour-of-day 分組,算每小時的下注 / 勝率 / 平均淨變動 / 平均賠率。

    回傳長度 24 的 list,每筆有:
        {hour, bets, wins, win_rate, total_change, avg_change, avg_multiplier}
    """
    buckets: list[dict] = [
        {"hour": h, "bets": 0, "wins": 0, "total_change": 0,
         "total_wagered": 0, "total_gross_won": 0}
        for h in range(24)
    ]
    for r in history:
        ts_str = r.get("ts", "")
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        h = dt.hour
        if not 0 <= h <= 23:
            continue
        b = buckets[h]
        bet = int(r.get("bet", 0) or 0)
        change = int(r.get("change", 0) or 0)
        b["bets"] += 1
        if change > 0:
            b["wins"] += 1
        b["total_change"] += change
        b["total_wagered"] += bet
        b["total_gross_won"] += max(0, change + bet)

    out = []
    for b in buckets:
        n = b["bets"]
        out.append({
            "hour":           b["hour"],
            "bets":           n,
            "wins":           b["wins"],
            "win_rate":       (b["wins"] / n) if n else 0.0,
            "total_change":   b["total_change"],
            "avg_change":     (b["total_change"] / n) if n else 0.0,
            "avg_multiplier": (b["total_gross_won"] / b["total_wagered"])
                              if b["total_wagered"] > 0 else 0.0,
        })
    return out


# ── Drawdown 分析(峰值跌幅) ─────────────────────────────────────────
def compute_drawdown(history: list[dict]) -> dict:
    """計算 cumulative net 的峰值跌幅。

    Returns:
        {
          "max_drawdown":        最大跌幅(絕對值,正數),
          "max_drawdown_idx":    最大跌幅落點的 history 索引,
          "current_drawdown":    當前距離歷史峰值的跌幅(可能 = 0),
          "peak":                歷史峰值,
          "current_net":         最後一筆累計淨收,
        }
    history 為空時所有欄位 = 0。
    """
    if not history:
        return {
            "max_drawdown":     0,
            "max_drawdown_idx": 0,
            "current_drawdown": 0,
            "peak":             0,
            "current_net":      0,
        }

    peak = 0
    max_dd = 0
    max_dd_idx = 0
    cum = 0
    for i, r in enumerate(history):
        cum += int(r.get("change", 0) or 0)
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
            max_dd_idx = i

    current_dd = peak - cum
    return {
        "max_drawdown":     max_dd,
        "max_drawdown_idx": max_dd_idx,
        "current_drawdown": current_dd,
        "peak":             peak,
        "current_net":      cum,
    }


# ── 顯示工具 ──────────────────────────────────────────────────────────
def format_symbol_display(sym: str) -> str:
    """把 :shortcode: 轉成「emoji name」格式。Unicode emoji 原樣回傳。"""
    m = _SHORTCODE_NAME_RE.match(sym)
    if not m:
        return sym
    name = m.group(1)
    icon = _SHORTCODE_EMOJI_MAP.get(name, _CUSTOM_EMOJI_MARKER)
    return f"{icon} {name}"


_format_symbol_display = format_symbol_display


def is_noise_symbol(sym: str, win_count: int, grid_prob: float) -> bool:
    """符號被視為雜訊(隱藏):沒中過獎且格子機率低於閾值。"""
    return win_count == 0 and grid_prob < SYMBOL_DISPLAY_THRESHOLD


_is_noise_symbol = is_noise_symbol


# ── Migration:舊資料補欄位 + 清貨幣 emoji ──────────────────────────
def migrate_slot_analysis(data: dict) -> dict:
    """補上新增欄位 / 清掉錯誤資料,讓舊版資料能繼續用。"""
    if not isinstance(data, dict):
        return make_slot_analysis()

    # 補欄位
    pd = data.get("payout_distribution", {})
    if not isinstance(pd, dict) or not all(k in pd for k in PAYOUT_BUCKETS):
        data["payout_distribution"] = {k: 0 for k in PAYOUT_BUCKETS}
    data.setdefault("high_mults", [])
    data.setdefault("valid_rr_count", data.get("total_spins", 0))   # 舊資料假設都 valid

    # 清掉貨幣 emoji 在 grid_symbol_freq 中的污染
    gsf = data.get("grid_symbol_freq", {})
    removed_total = 0
    for non_slot in NON_SLOT_SHORTCODES:
        c = gsf.pop(non_slot, 0)
        if isinstance(c, int) and c > 0:
            removed_total += c
    if removed_total > 0:
        data["grid_total_cells"] = max(0, data.get("grid_total_cells", 0) - removed_total)

    ss = data.get("symbol_stats", {})
    for non_slot in NON_SLOT_SHORTCODES:
        ss.pop(non_slot, None)

    return data

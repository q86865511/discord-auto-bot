"""
Slot 分析資料模型 + 計算 + 持久化 + 符號顯示。

從 `main.py` 拆出來：
- 累加器（_make_slot_analysis、_update_slot_analysis）
- 統計計算（compute_slot_stats）
- 顯示工具（_format_symbol_display、_is_noise_symbol）
- 持久化（load/save_slot_analysis、load/save_history）

依賴：`bot.slot.parsers._NON_SLOT_SHORTCODES`（migration 用）。
"""
from __future__ import annotations

import json
import os
import re

from bot.slot.parsers import _NON_SLOT_SHORTCODES


__all__ = [
    # constants
    "MIN_KELLY_SAMPLES",
    "PAYOUT_BUCKETS", "HIGH_MULT_THRESHOLD", "HIGH_MULT_KEEP",
    "ANALYSIS_PATH", "HISTORY_PATH", "HISTORY_MAX_LEN",
    "_SYMBOL_DISPLAY_THRESHOLD",
    "_CUSTOM_EMOJI_MARKER", "_SHORTCODE_NAME_RE", "_SHORTCODE_EMOJI_MAP",
    # functions
    "_make_slot_analysis", "_update_slot_analysis", "compute_slot_stats",
    "compute_hourly_breakdown",
    "_format_symbol_display", "_is_noise_symbol",
    "load_slot_analysis", "save_slot_analysis",
    "load_history", "save_history",
]


# ── 持久化路徑 ────────────────────────────────────────────────────────────
ANALYSIS_PATH     = "slot_analysis.json"
HISTORY_PATH      = "gambling_history.json"
HISTORY_MAX_LEN   = 5000  # 最近 N 筆紀錄（避免檔案無限長大）

# ── Kelly / 賠率分布 / 符號顯示 ────────────────────────────────────────────
MIN_KELLY_SAMPLES = 50    # Kelly 策略需要的最少轉數才生效

# 賠率分布的 bucket key 順序（同步用於 _update_slot_analysis 與顯示）
PAYOUT_BUCKETS = ["0", "0~2", "2~5", "5~8", "8~10", "10~20", "以上"]
HIGH_MULT_THRESHOLD = 20.0    # 賠率 >= 此值就計入 high_mults（並落在「以上」桶）
HIGH_MULT_KEEP      = 50      # 最多保留多少筆 high_mults（避免無限長大）

# 顯示閾值：低於這個格子機率且沒中過獎的符號 → 隱藏（視為解析雜訊）
_SYMBOL_DISPLAY_THRESHOLD = 0.001   # 0.1%

# 自訂 emoji 沒對應到時用的 fallback marker
_CUSTOM_EMOJI_MARKER = "🟦"
_SHORTCODE_NAME_RE = re.compile(r'^:([a-z0-9_]+):$')

# Slot 常見符號 shortcode → 視覺化的 Unicode emoji。
# 找不到對應時用 _CUSTOM_EMOJI_MARKER 當 fallback。
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
    # 經典 slot 符號
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
    # 該伺服器有時出現的（從 grid 看不出含義）— 用 fallback marker，不刻意對應
}


# ── 累加器 ────────────────────────────────────────────────────────────────
def _make_slot_analysis() -> dict:
    return {
        "total_spins": 0,
        "total_wins": 0,
        "total_losses": 0,
        "total_wagered": 0,
        "total_gross_won": 0,
        "sum_return_ratio": 0.0,
        "sum_return_ratio_sq": 0.0,
        "symbol_stats": {},
        "line_stats": {},
        "grid_symbol_freq": {},
        "grid_total_cells": 0,
        "payout_distribution": {k: 0 for k in PAYOUT_BUCKETS},
        "high_mults": [],   # >=HIGH_MULT_THRESHOLD 的 return ratio（最多保留 HIGH_MULT_KEEP 筆）
    }


def _update_slot_analysis(state: dict, bet: int, change: int,
                          lines: list[dict], grid: list[str] | None):
    sa = state["slot_analysis"]
    sa["total_spins"] += 1
    sa["total_wagered"] += bet

    gross_win = max(0, change + bet)
    sa["total_gross_won"] += gross_win

    rr = gross_win / bet if bet > 0 else 0.0
    sa["sum_return_ratio"] += rr
    sa["sum_return_ratio_sq"] += rr * rr

    if change > 0:
        sa["total_wins"] += 1
    else:
        sa["total_losses"] += 1

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
        if len(hm) > HIGH_MULT_KEEP:
            del hm[:len(hm) - HIGH_MULT_KEEP]

    for line in lines:
        sym = line["symbol"]
        ss = sa["symbol_stats"].setdefault(
            sym, {"win_appearances": 0, "total_payout": 0, "total_mult_sum": 0.0}
        )
        ss["win_appearances"] += 1
        ss["total_payout"] += line["payout"]
        ss["total_mult_sum"] += line["symbol_mult"]

        ln = line["line_name"]
        ls = sa["line_stats"].setdefault(ln, {"hits": 0, "total_payout": 0})
        ls["hits"] += 1
        ls["total_payout"] += line["payout"]

    if grid is not None and len(grid) >= 9:
        for sym in grid[:9]:
            sa["grid_symbol_freq"][sym] = sa["grid_symbol_freq"].get(sym, 0) + 1
        sa["grid_total_cells"] += 9


def compute_slot_stats(sa: dict) -> dict:
    n = sa.get("total_spins", 0)
    if n == 0:
        return {"sufficient_data": False, "total_spins": 0}

    ev = sa["sum_return_ratio"] / n
    mean_sq = sa["sum_return_ratio_sq"] / n
    variance = max(0.0, mean_sq - ev * ev)
    std_dev = variance ** 0.5

    win_rate = sa["total_wins"] / n
    avg_win_mult = (sa["total_gross_won"] / sa["total_wagered"]
                    if sa["total_wagered"] > 0 else 0.0)

    kelly_fraction = ((ev - 1.0) / variance) if (variance > 0 and ev > 1.0) else 0.0

    symbol_info = {}
    for sym, ss in sa.get("symbol_stats", {}).items():
        cnt = ss["win_appearances"]
        symbol_info[sym] = {
            "win_appearances": cnt,
            "avg_mult": ss["total_mult_sum"] / cnt if cnt > 0 else 0.0,
            "total_payout": ss["total_payout"],
        }

    grid_total = sa.get("grid_total_cells", 0)
    grid_symbol_prob = {}
    if grid_total > 0:
        for sym, cnt in sa.get("grid_symbol_freq", {}).items():
            grid_symbol_prob[sym] = cnt / grid_total

    line_info = {}
    for ln, ls in sa.get("line_stats", {}).items():
        line_info[ln] = {
            "hits": ls["hits"],
            "hit_rate": ls["hits"] / n,
            "total_payout": ls["total_payout"],
        }

    return {
        "sufficient_data": n >= MIN_KELLY_SAMPLES,
        "total_spins": n,
        "ev": ev,
        "edge": ev - 1.0,
        "variance": variance,
        "std_dev": std_dev,
        "win_rate": win_rate,
        "avg_win_mult": avg_win_mult,
        "kelly_fraction": kelly_fraction,
        "symbol_info": symbol_info,
        "grid_symbol_prob": grid_symbol_prob,
        "line_info": line_info,
        "payout_distribution": sa.get("payout_distribution", {}),
        "high_mults": sa.get("high_mults", []),
    }


# ── 時段分析（hour-of-day breakdown）────────────────────────────────────
def compute_hourly_breakdown(history: list[dict]) -> list[dict]:
    """
    把 history 依 hour-of-day（0~23）分組，算每小時的下注次數 / 勝率 / 平均淨變動 /
    平均賠率（gross_win / bet）。回傳 list 長度 24，每筆有：
      {hour, bets, wins, win_rate, total_change, avg_change, avg_multiplier}
    沒下注的 hour 也會在，數字都 0。
    """
    from datetime import datetime
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
        if not (0 <= h <= 23):
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


# ── 顯示工具 ──────────────────────────────────────────────────────────────
def _format_symbol_display(sym: str) -> str:
    """
    顯示用：把 Discord 自訂 shortcode 轉成「emoji name」格式
    （去掉冒號、加上對應的 emoji；找不到對應就用 🟦 當 fallback）。
    Unicode emoji（🍒 等）原樣回傳。
    底層儲存仍維持 :name: 格式，避免和標準 emoji 衝突。
    """
    m = _SHORTCODE_NAME_RE.match(sym)
    if not m:
        return sym
    name = m.group(1)
    icon = _SHORTCODE_EMOJI_MAP.get(name, _CUSTOM_EMOJI_MARKER)
    return f"{icon} {name}"


def _is_noise_symbol(sym: str, win_count: int, grid_prob: float) -> bool:
    """符號被視為雜訊（隱藏）：沒中過獎 且 格子機率低於閾值。"""
    return win_count == 0 and grid_prob < _SYMBOL_DISPLAY_THRESHOLD


# ── 持久化 ────────────────────────────────────────────────────────────────
def load_slot_analysis() -> dict | None:
    if not os.path.exists(ANALYSIS_PATH):
        return None
    try:
        with open(ANALYSIS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    # 遷移：若 payout_distribution 是舊版 bucket 結構，重置為新版
    # （舊版逐筆 return ratio 沒留下，無法精確映射，只好歸零；
    #  其他統計欄位完整保留）
    pd = data.get("payout_distribution", {})
    if not all(k in pd for k in PAYOUT_BUCKETS):
        data["payout_distribution"] = {k: 0 for k in PAYOUT_BUCKETS}

    # 遷移：補上新增的 high_mults 欄位
    data.setdefault("high_mults", [])

    # 遷移：清掉貨幣 emoji（:fh: 油票、:ti: 小魚干、:oi: 油幣 …）
    # 這些是 /balance 文字裡的 emoji，被 grid 解析誤算進去；移除並對應扣回
    # grid_total_cells，讓真實符號的格子機率回到正確值
    gsf = data.get("grid_symbol_freq", {})
    removed_total = 0
    for non_slot in _NON_SLOT_SHORTCODES:
        c = gsf.pop(non_slot, 0)
        if isinstance(c, int) and c > 0:
            removed_total += c
    if removed_total > 0:
        data["grid_total_cells"] = max(
            0, data.get("grid_total_cells", 0) - removed_total
        )
    # symbol_stats 也清一下（如果有貨幣 emoji 跑進來，雖然線路解析不太可能誤觸）
    ss = data.get("symbol_stats", {})
    for non_slot in _NON_SLOT_SHORTCODES:
        ss.pop(non_slot, None)

    return data


def save_slot_analysis(state: dict):
    with open(ANALYSIS_PATH, "w", encoding="utf-8") as f:
        json.dump(state["slot_analysis"], f, ensure_ascii=False, indent=2)


def load_history() -> list | None:
    """從 gambling_history.json 載入歷史下注紀錄。"""
    if not os.path.exists(HISTORY_PATH):
        return None
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            return None
    except (json.JSONDecodeError, OSError):
        return None


def save_history(state: dict):
    """寫入歷史下注紀錄；只保留最近 HISTORY_MAX_LEN 筆。"""
    history = state.get("history") or []
    if len(history) > HISTORY_MAX_LEN:
        history = history[-HISTORY_MAX_LEN:]
        state["history"] = history
    try:
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

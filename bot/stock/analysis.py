"""股票簡易技術分析 + 買/賣建議。

純啟發式,不是金融建議:
- 買進訊號:現價 < 短期均線 < 長期均線 + 短期 momentum 轉正
- 賣出訊號(對持股):
    * 已獲利 ≥ take_profit_pct → 建議賣
    * 跌破 stop_loss_pct → 建議賣(止損)
    * 現價 > 短期均線 > 長期均線 + 連 N 日下跌 → 建議賣

回傳格式統一:dict 含 signal ∈ {"buy", "sell", "hold"}, score(0~100), reason。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)


# ── 統計輔助 ──────────────────────────────────────────────────────────
def _moving_average(prices: list[float], window: int) -> float | None:
    if len(prices) < window or window <= 0:
        return None
    return sum(prices[-window:]) / window


def _stddev(prices: list[float]) -> float:
    if len(prices) < 2:
        return 0.0
    m = sum(prices) / len(prices)
    var = sum((p - m) ** 2 for p in prices) / (len(prices) - 1)
    return var ** 0.5


def _pct_change(curr: float, prev: float) -> float:
    if prev <= 0:
        return 0.0
    return (curr - prev) / prev * 100


# ── 短期波動偵測 ──────────────────────────────────────────────────────
def detect_volatility(
    series: list[dict],
    window_min: float,
    threshold_pct: float,
) -> dict | None:
    """檢查 series 在過去 window_min 分鐘內的價格變動是否 ≥ threshold_pct。

    series:[{ts, symbol, price}, ...] 升序(舊→新)。ts 格式 "%Y-%m-%d %H:%M:%S"。
    回傳:
      None — 樣本不足或變動沒超過閾值
      {direction, change_pct, current, baseline, baseline_ts, window_min}
        direction ∈ {"rise", "fall"}, change_pct 是有號(rise 正、fall 負)。

    比較方式:取 series 最新價當「現價」,在 (現在 − window_min) 分鐘內的
    最早一筆當 baseline。如果窗口內只有 1 筆資料就放棄(沒得比)。
    """
    if not series or len(series) < 2:
        return None
    if window_min <= 0 or threshold_pct <= 0:
        return None

    latest = series[-1]
    cur_price = float(latest.get("price") or 0)
    if cur_price <= 0:
        return None

    # 解析 latest 的 ts 當「現在」(用 series 自己的時間軸,不靠 wall-clock,
    # 避免 bot 啟動時 history 沒新資料但 wall-clock 一直走造成 baseline 抓不到)
    cur_ts_str = latest.get("ts") or ""
    try:
        cur_ts = datetime.strptime(cur_ts_str, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    cutoff = cur_ts - timedelta(minutes=window_min)

    # 從新到舊掃,找第一個 ts <= cutoff 的;若窗口內全部都比 cutoff 新就用最舊那筆
    baseline_row = None
    for row in reversed(series[:-1]):
        try:
            row_ts = datetime.strptime(row.get("ts") or "", "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            continue
        if row_ts <= cutoff:
            baseline_row = row
            break
    # fallback:窗口比 series 還短(剛收集),用最舊一筆當 baseline
    if baseline_row is None:
        baseline_row = series[0]
        try:
            baseline_ts = datetime.strptime(
                baseline_row.get("ts") or "", "%Y-%m-%d %H:%M:%S",
            )
        except (TypeError, ValueError):
            return None
        # 窗口內至少要有 5 分鐘資料才有意義(避免剛開機就誤觸)
        if (cur_ts - baseline_ts).total_seconds() < 5 * 60:
            return None

    baseline_price = float(baseline_row.get("price") or 0)
    if baseline_price <= 0:
        return None

    change_pct = (cur_price - baseline_price) / baseline_price * 100
    if abs(change_pct) < threshold_pct:
        return None
    # Sanity:股票 N 分鐘內絕對不該變動 > 1000%。這量級多半是 parser 誤
    # 抓到另一支股的價格寫入 series(例如 MAID 27000 跟 WAVE 60 混了)。
    # 跳過通知避免發出「暴漲 43000%」的鬧劇 email。
    if abs(change_pct) > 1000:
        log.warning(
            "detect_volatility: %s 變動 %.1f%% 超過 1000%% — 棄用此次警示"
            "(疑似 parser 異常,baseline=%.4f current=%.4f)",
            (series[-1].get("symbol") or "?"), change_pct,
            baseline_price, cur_price,
        )
        return None

    return {
        "direction":   "rise" if change_pct > 0 else "fall",
        "change_pct":  change_pct,
        "current":     cur_price,
        "baseline":    baseline_price,
        "baseline_ts": baseline_row.get("ts") or "",
        "window_min":  window_min,
    }


# ── 把 history rows 轉成 per-symbol price series ─────────────────────
def group_by_symbol(history: list[dict]) -> dict[str, list[dict]]:
    """history 是 [{ts, symbol, price}, ...](升序),回傳 {symbol: [rows...]}。"""
    out: dict[str, list[dict]] = defaultdict(list)
    for r in history:
        sym = r.get("symbol")
        if sym:
            out[sym].append(r)
    return dict(out)


# ── 買進建議 ──────────────────────────────────────────────────────────
def buy_signal(
    series: list[dict],
    ma_short: int = 5,
    ma_long: int = 20,
    momentum_window: int = 3,
) -> dict[str, Any]:
    """以單一 symbol 的價格序列判斷是否該買。

    對齊技術分析慣例,讓買 / 賣訊號互斥(同一形態不會雙邊都加分):
      - 黃金交叉(短均 > 長均)= 多頭 → 加分
      - 死亡交叉(短均 < 長均)= 空頭 → 扣分
      - buy-the-dip:遠低於長均 + 近期 momentum 翻正 = 加分
      - 純跌不接:近期 momentum 仍負 = 扣分

    series 是 [{ts, price}, ...] 升序。需要至少 max(ma_long, momentum_window+1) 筆。
    """
    prices = [r["price"] for r in series]
    n = len(prices)
    min_n = max(ma_long, momentum_window + 1)
    if n < min_n:
        return {
            "signal": "hold",
            "score": 0,
            "reason": f"資料不足({n}/{min_n})",
        }

    cur = prices[-1]
    ma_s = _moving_average(prices, ma_short)
    ma_l = _moving_average(prices, ma_long)
    sd = _stddev(prices[-ma_long:])

    # 短期 momentum:最近 N 筆有沒有上升
    recent = prices[-momentum_window-1:]
    pct_recent = _pct_change(recent[-1], recent[0])

    score = 50    # 中性
    reasons: list[str] = []

    # ── 趨勢方向(主要訊號) ───────────────────────────────────────
    if ma_s is not None and ma_l is not None:
        # 0.2% 緩衝,避免兩條線幾乎相等時雜訊閃爍
        ratio = ma_s / ma_l if ma_l > 0 else 1.0
        if ratio > 1.002:
            score += 15
            reasons.append(f"短均 {ma_s:.2f} > 長均 {ma_l:.2f}(多頭趨勢)")
        elif ratio < 0.998:
            score -= 15
            reasons.append(f"短均 {ma_s:.2f} < 長均 {ma_l:.2f}(空頭趨勢,不宜接)")

    # ── 短期動能 ────────────────────────────────────────────────────
    if ma_s is not None:
        if cur > ma_s * 1.005:
            score += 5
            reasons.append("現價 > 短均(短期動能向上)")
        elif cur < ma_s * 0.995:
            score -= 5
            reasons.append("現價 < 短均(短期走弱)")

    # ── Buy-the-dip:遠低於長均但 momentum 開始翻正 ─────────────────
    if ma_l is not None and cur < ma_l * 0.95 and pct_recent > 1.0:
        score += 15
        reasons.append(f"現價 < 長均 -5% 但近期反彈 +{pct_recent:.1f}%(可能築底)")

    # ── 動能 ────────────────────────────────────────────────────────
    if pct_recent > 2.0:
        score += 10
        reasons.append(f"近 {momentum_window} 筆漲 +{pct_recent:.1f}%")
    elif pct_recent < -3.0:
        score -= 15
        reasons.append(f"近 {momentum_window} 筆跌 {pct_recent:.1f}%(動能為負)")

    # 高波動扣分
    if sd > cur * 0.10:
        score -= 10
        reasons.append(f"波動度高 σ={sd:.2f}")

    score = max(0, min(100, score))

    if score >= 70:
        signal = "buy"
    elif score <= 30:
        signal = "avoid"
    else:
        signal = "hold"

    return {
        "signal":   signal,
        "score":    score,
        "reason":   "; ".join(reasons) if reasons else "持平",
        "current":  cur,
        "ma_short": ma_s,
        "ma_long":  ma_l,
        "stddev":   sd,
        "n":        n,
    }


# ── 賣出建議(對持股) ───────────────────────────────────────────────
def sell_signal(
    series: list[dict],
    avg_cost: float,
    take_profit_pct: float = 15.0,
    stop_loss_pct: float = 10.0,
    ma_short: int = 5,
    ma_long: int = 20,
) -> dict[str, Any]:
    """對持股判斷是否該賣。

    series 是 [{ts, price}, ...] 升序。avg_cost 是平均持有成本。
    """
    prices = [r["price"] for r in series]
    n = len(prices)
    if n == 0:
        return {"signal": "hold", "score": 0, "reason": "無報價資料"}

    cur = prices[-1]
    if avg_cost > 0:
        profit_pct = _pct_change(cur, avg_cost)
    else:
        profit_pct = 0.0

    score = 50
    reasons: list[str] = []

    # 強烈訊號:獲利達標 / 跌破停損
    if avg_cost > 0:
        if profit_pct >= take_profit_pct:
            score = 95
            reasons.append(
                f"獲利 {profit_pct:+.1f}% ≥ {take_profit_pct:.0f}% → 建議獲利了結"
            )
            return {
                "signal": "sell", "score": score, "reason": "; ".join(reasons),
                "current": cur, "avg_cost": avg_cost, "profit_pct": profit_pct,
            }
        if profit_pct <= -stop_loss_pct:
            score = 90
            reasons.append(
                f"虧損 {profit_pct:.1f}% ≤ -{stop_loss_pct:.0f}% → 建議停損"
            )
            return {
                "signal": "sell", "score": score, "reason": "; ".join(reasons),
                "current": cur, "avg_cost": avg_cost, "profit_pct": profit_pct,
            }

    # 趨勢訊號:價格已 < 短均(轉跌)
    if n >= ma_long:
        ma_s = _moving_average(prices, ma_short)
        ma_l = _moving_average(prices, ma_long)
        if ma_s is not None and ma_l is not None:
            if cur < ma_s and ma_s < ma_l:
                score += 15
                reasons.append("現價 < 短均 < 長均 → 趨勢轉空")

    # 連續下跌
    if n >= 4:
        recent_drops = sum(
            1 for i in range(-3, 0)
            if prices[i] < prices[i-1]
        )
        if recent_drops >= 3:
            score += 10
            reasons.append("近 3 筆連跌")

    if avg_cost > 0:
        reasons.insert(0, f"目前獲利 {profit_pct:+.1f}%")

    if score >= 65:
        signal = "sell"
    elif score <= 35:
        signal = "buy_more"
    else:
        signal = "hold"

    return {
        "signal":     signal,
        "score":      score,
        "reason":     "; ".join(reasons) if reasons else "持平",
        "current":    cur,
        "avg_cost":   avg_cost,
        "profit_pct": profit_pct if avg_cost > 0 else None,
    }


# ── 統合分析:給某個 symbol(包含當下未持股 / 已持股兩種建議) ─────
def analyze_symbol(
    symbol: str, series: list[dict],
    held_shares: float = 0.0, avg_cost: float = 0.0,
    cfg: Any = None,
) -> dict[str, Any]:
    """給一支股票完整評估。

    cfg 是 StockConfig dataclass(或 None 用預設值)。
    回傳 dict 含 buy_eval / sell_eval(若已持有);若未持有 sell_eval=None。
    """
    ma_s = int(getattr(cfg, "ma_short", 5)) if cfg else 5
    ma_l = int(getattr(cfg, "ma_long", 20)) if cfg else 20
    tp_pct = float(getattr(cfg, "take_profit_pct", 15.0)) if cfg else 15.0
    sl_pct = float(getattr(cfg, "stop_loss_pct", 10.0)) if cfg else 10.0

    buy_eval = buy_signal(series, ma_short=ma_s, ma_long=ma_l)
    sell_eval = None
    if held_shares > 0:
        sell_eval = sell_signal(
            series, avg_cost,
            take_profit_pct=tp_pct, stop_loss_pct=sl_pct,
            ma_short=ma_s, ma_long=ma_l,
        )

    return {
        "symbol":       symbol,
        "held_shares":  held_shares,
        "avg_cost":     avg_cost,
        "buy_eval":     buy_eval,
        "sell_eval":    sell_eval,
        "n_samples":    len(series),
    }

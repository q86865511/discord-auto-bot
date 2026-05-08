"""股票文字解析 — 從 Discord embed text 抓 symbol/price/holdings。

設計原則:
- 不知道使用者環境的確切 /stock embed 格式 → 採用「多 pattern 容錯」
- 解析失敗時 dump 完整 raw text 到 SLOT_DEBUG_LOG_PATH-ish 位置
- 使用者可在 config.stock.parse_patterns 自訂 regex(可空,fallback 到內建)

預設支援的格式(常見 economy bot):
    AAPL: $123.45
    AAPL  $123.45
    [AAPL] 123.45
    AAPL — 123.45
    AAPL Price: 123.45
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable

log = logging.getLogger(__name__)


# ── Symbol + price 解析 ─────────────────────────────────────────────
# 多種 pattern,逐一嘗試。Symbol 是 1-6 個英數字大寫字母。
# Price 接受帶 $、千分位逗號、小數點。
DEFAULT_PRICE_PATTERNS = [
    # AAPL: $123.45  或  AAPL: 123.45
    re.compile(r"\b([A-Z]{1,6})\s*[:\-—]\s*\$?\s*([0-9][0-9,]*\.?\d*)"),
    # [AAPL] 123.45
    re.compile(r"\[([A-Z]{1,6})\]\s*\$?\s*([0-9][0-9,]*\.?\d*)"),
    # AAPL Price: 123.45 或 AAPL price 123.45
    re.compile(r"\b([A-Z]{1,6})\s+(?:price|Price)[\s:]+\$?\s*([0-9][0-9,]*\.?\d*)"),
    # 純表格:AAPL  ⋮  123.45  (空白分隔,符號在前)
    re.compile(r"^\s*([A-Z]{2,6})\s+\$?([0-9][0-9,]*\.?\d*)\s*$", re.MULTILINE),
]


def _parse_number(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except (ValueError, TypeError):
        return None


def parse_stock_prices(
    text: str, custom_patterns: Iterable[str] | None = None,
) -> dict[str, float]:
    """從整頁文字抓所有 (symbol, price) pair。

    custom_patterns:使用者可給額外 regex(每個須有 2 個 group:symbol, price)。
    """
    if not text:
        return {}

    patterns = list(DEFAULT_PRICE_PATTERNS)
    if custom_patterns:
        for pat in custom_patterns:
            try:
                patterns.append(re.compile(pat))
            except re.error as e:
                log.warning("使用者 custom pattern 編譯失敗 %r: %s", pat, e)

    found: dict[str, float] = {}
    for pat in patterns:
        for m in pat.finditer(text):
            sym = m.group(1).upper()
            price = _parse_number(m.group(2))
            if price is None or price <= 0:
                continue
            # 簡單衛生:濾掉一些誤抓的英文單字
            if sym in {"USD", "USDT", "PRICE", "TOTAL", "BUY", "SELL", "STOCK"}:
                continue
            # 第一個 pattern 抓到的優先;後面 pattern 不覆蓋
            found.setdefault(sym, price)

    return found


# ── Holdings 解析 ──────────────────────────────────────────────────────
# 常見格式:
#   AAPL: 10 shares @ $120.00
#   AAPL × 10  ($120.00)
#   AAPL: 10 stocks
#   You own 10 AAPL @ $120
DEFAULT_HOLDING_PATTERNS = [
    # symbol: N shares @ avg
    re.compile(
        r"\b([A-Z]{1,6})[:\s]+(\d+(?:\.\d+)?)\s*(?:shares?|stocks?|股)"
        r"(?:\s*@\s*\$?\s*([0-9][0-9,]*\.?\d*))?",
        re.IGNORECASE,
    ),
    # symbol × N  ($avg)
    re.compile(
        r"\b([A-Z]{1,6})\s*[×x]\s*(\d+(?:\.\d+)?)"
        r"(?:\s*\(\s*\$?\s*([0-9][0-9,]*\.?\d*)\s*\))?",
    ),
    # You own N AAPL @ $avg
    re.compile(
        r"(?:own|持有)\s+(\d+(?:\.\d+)?)\s+([A-Z]{1,6})"
        r"(?:\s*@\s*\$?\s*([0-9][0-9,]*\.?\d*))?",
    ),
]


def parse_holdings(text: str) -> dict[str, dict]:
    """抓持股。回傳 {symbol: {shares, avg_cost or 0}}。"""
    if not text:
        return {}
    found: dict[str, dict] = {}
    for pat in DEFAULT_HOLDING_PATTERNS:
        for m in pat.finditer(text):
            groups = m.groups()
            # 第三個 pattern 是 (shares, sym, avg);其他是 (sym, shares, avg)
            if pat is DEFAULT_HOLDING_PATTERNS[2]:
                shares_str, sym, avg_str = groups[0], groups[1], groups[2]
            else:
                sym, shares_str, avg_str = groups[0], groups[1], groups[2]
            sym = sym.upper()
            if sym in {"USD", "USDT", "TOTAL", "STOCK"}:
                continue
            shares = _parse_number(shares_str)
            if shares is None or shares <= 0:
                continue
            avg = _parse_number(avg_str) if avg_str else 0.0
            existing = found.get(sym)
            if existing is None or existing["shares"] < shares:
                found[sym] = {"shares": shares, "avg_cost": avg or 0.0}
    return found

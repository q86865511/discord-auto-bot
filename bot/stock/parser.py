"""股票文字解析 — 抓 Discord embed 中的 symbol/price/holdings。

兩種來源:
1. /portfolio embed —— 一次拿到全部持股 + 現價(最有用)
2. /stock symbol:XXX embed —— 單一股票的詳細價格

實際格式範例(from 截圖):

/portfolio:
    HOLO (Hololive)
        持有: 100 股
        均買價: 479.44
        現價: 476.95
        市值: 47695.34
        盈虧: -248.87

/stock symbol: HOLO:
    HOLO - Hololive
    全球領先的虛擬偶像娛樂集團...
    當前價格   基礎波動率   波動放大因子
    476.95 油幣  0.007       1.20
    ...
    您持有 HOLO
    100 股

Autocomplete dropdown(打 `/stock symbol:` 時 Discord 顯示):
    AZGC - 亞馬遜雲創 (36.76 油幣)
    GCR - 嘎核心指標 (65.52 油幣)
    HOLO - Hololive (476.95 油幣)
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


def _parse_number(s: str) -> float | None:
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


# ── /portfolio 解析(主要來源) ──────────────────────────────────────
# 每筆 holding entry 大致長這樣(順序固定):
#   HOLO (Hololive)
#       持有: 100 股
#       均買價: 479.44
#       現價: 476.95
#       市值: 47695.34
#       盈虧: -248.87
#
# 用「symbol header + 持有/均買價/現價」去抓
# Symbol header:大寫字母 + 中括號公司名,後面接「持有: N 股」
PORTFOLIO_HOLDING_BLOCK = re.compile(
    r"([A-Z][A-Z0-9]{1,6})\s*\([^)]+\)"          # SYMBOL (Name)
    r".*?持有[:\s]*([0-9]+(?:\.[0-9]+)?)\s*股"   # 持有: N 股
    r".*?均買價[:\s]*\$?([0-9,]+(?:\.[0-9]+)?)"  # 均買價: X.XX
    r".*?現價[:\s]*\$?([0-9,]+(?:\.[0-9]+)?)",   # 現價: Y.YY
    re.DOTALL,
)


def parse_portfolio(text: str) -> dict[str, dict]:
    """從 /portfolio 抓所有持股,順便回傳現價。

    回傳 {symbol: {shares, avg_cost, current_price}}。
    """
    if not text:
        return {}
    out: dict[str, dict] = {}
    for m in PORTFOLIO_HOLDING_BLOCK.finditer(text):
        sym = m.group(1).upper()
        shares = _parse_number(m.group(2))
        avg = _parse_number(m.group(3))
        cur = _parse_number(m.group(4))
        if shares is None or shares <= 0:
            continue
        out[sym] = {
            "shares":        shares,
            "avg_cost":      avg or 0.0,
            "current_price": cur or 0.0,
        }
    return out


# ── /stock symbol:XXX embed 解析 ─────────────────────────────────────
# 抓 "當前價格\n.../n476.95 油幣" 這個 pattern
# Embed 順序:當前價格 / 基礎波動率 / 波動放大因子,底下接 NUMBER 油幣 …
# 所以 "當前價格" 後第一個出現的 "X 油幣" 就是現價
STOCK_DETAIL_PRICE = re.compile(
    r"當前價格[\s\S]{0,200}?([0-9,]+(?:\.[0-9]+)?)\s*油幣",
)
# 您持有 SYMBOL\n  N 股
STOCK_DETAIL_HOLDING = re.compile(
    r"您持有\s+([A-Z][A-Z0-9]{1,6})[\s\S]{0,80}?([0-9]+(?:\.[0-9]+)?)\s*股",
)
# Symbol/name header(在 embed 最上方),例如 "HOLO - Hololive"
STOCK_DETAIL_HEADER = re.compile(
    r"\b([A-Z][A-Z0-9]{1,6})\s*[-—–]\s*[一-鿿A-Za-z]"
)


def parse_stock_detail(text: str, expected_symbol: str | None = None) -> dict | None:
    """從 /stock symbol:X 的 embed 抓 {symbol, price, held_shares}。

    expected_symbol:呼叫端知道自己問的是哪支,用來校驗 + 萬一 header 抓不到時補。
    """
    if not text:
        return None

    price_m = STOCK_DETAIL_PRICE.search(text)
    if not price_m:
        return None
    price = _parse_number(price_m.group(1))
    if price is None or price <= 0:
        return None

    # Symbol:優先從 "您持有 SYM" 抓(最可靠);其次從 header 第一行
    sym = None
    held = 0.0
    held_m = STOCK_DETAIL_HOLDING.search(text)
    if held_m:
        sym = held_m.group(1).upper()
        held = _parse_number(held_m.group(2)) or 0.0
    if not sym:
        hdr = STOCK_DETAIL_HEADER.search(text)
        if hdr:
            sym = hdr.group(1).upper()
    if not sym and expected_symbol:
        sym = expected_symbol.upper()
    if not sym:
        return None

    return {
        "symbol":       sym,
        "current_price": price,
        "held_shares":   held,
    }


# ── /stock(無 symbol param)回應的 embed 解析 ────────────────────────
# 實際格式(from log dump):
#     股市行情 (第 1/1 頁)
#     AZGC - 亞馬遜雲創
#      價格 : 36.71
#      趨勢 : -0.15%
#      成交量 : 323,142 股
#     GCR - 嘎核心指標
#      價格 : 65.48
#      ...
# 一次拿全部 10 支股票,比 autocomplete dropdown 可靠得多。
STOCK_LIST_ENTRY = re.compile(
    r"\b([A-Z][A-Z0-9]{1,6})\s*-\s*\S[^\n]{0,40}?"   # SYMBOL - 名字
    r"[\s\n]+價格\s*[:：]\s*\$?([0-9,]+(?:\.[0-9]+)?)",  # 價格 : NUMBER
    re.DOTALL,
)
# 舊名 dropdown 格式(`AZGC - 亞馬遜雲創 (36.76 油幣)`)— 留作備援
DROPDOWN_LINE = re.compile(
    r"([A-Z][A-Z0-9]{1,6})\s*-\s*[^()]+?\s*\(([0-9,]+(?:\.[0-9]+)?)\s*油幣\)",
)


def parse_stock_list(text: str) -> dict[str, float]:
    """從 `/stock`(無 symbol)的 embed 抓 {symbol: price}。

    優先用 STOCK_LIST_ENTRY(實際 embed 格式);若沒抓到再 fallback 試
    DROPDOWN_LINE(autocomplete 格式)。
    """
    if not text:
        return {}
    out: dict[str, float] = {}
    for m in STOCK_LIST_ENTRY.finditer(text):
        sym = m.group(1).upper()
        if sym in {"USD", "USDT", "TOTAL", "STOCK"}:
            continue
        price = _parse_number(m.group(2))
        if price is None or price <= 0:
            continue
        out.setdefault(sym, price)

    if not out:    # fallback to dropdown format
        for m in DROPDOWN_LINE.finditer(text):
            sym = m.group(1).upper()
            price = _parse_number(m.group(2))
            if price is None or price <= 0:
                continue
            out.setdefault(sym, price)

    return out


# 向後相容別名
def parse_stock_dropdown(text: str) -> dict[str, float]:
    return parse_stock_list(text)

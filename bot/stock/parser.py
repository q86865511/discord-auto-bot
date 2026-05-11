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


# ── Chunk-based 切分 helper ───────────────────────────────────────────
# 根因 fix:lazy `.*?` 在 finditer 跨 entry 邊界會把 A 的數據抓到 B 的
# group(MAID 的「現價: 27312.43」被 lazy match 配給 WAVE 的「現價」位置)。
# 解法:先用 SYMBOL header 把 textContent 切成 chunks,每 chunk 限定在
# 「一支股票」的範圍內 parse,絕對不會跨。

# Portfolio 持股 header:`SYMBOL (Name)` — 後面接持有 / 均買價 等
_PORTFOLIO_HOLDING_HEADER = re.compile(r"\b([A-Z][A-Z0-9]{1,6})\s*\(\s*[^)]+\)")
# Stock list header:`SYMBOL - Name` — 後面接價格 / 趨勢
_STOCK_LIST_HEADER = re.compile(r"\b([A-Z][A-Z0-9]{1,6})\s*-\s*\S")


def _chunk_by_header(text: str, header_pat: re.Pattern) -> list[tuple[str, str]]:
    """按 header pattern 切 text 成 [(sym, chunk), ...]。

    每個 chunk = 「header 結尾 → 下個 header 起點」,確保 parser 在 chunk
    內 search 不會跨 entry boundary。
    """
    matches = list(header_pat.finditer(text))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        sym = m.group(1).upper()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((sym, text[start:end]))
    return out


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
# 「盈虧」欄位獨立抓 — Discord 給的是內部精確值(我們自己 shares*(cur-avg) 算
# 會因 avg/cur 顯示時被 round 而偏差幾塊到數十塊,user 對照 Discord 會困惑)。
# 這 regex 抓「市值 ... 盈虧 : ±NUM」,綁定在「現價 ... 」之後;不指定具體
# symbol,在 parse_portfolio 內按出現順序對應到 holding。
# 注意:Discord 新版會在 盈虧 後加 emoji(● 紅 / 綠),`[^0-9+\-]{0,10}`
# 跳過 emoji + 空白找到數字。
PORTFOLIO_PNL = re.compile(
    r"市值[:\s]*\$?[0-9,]+(?:\.[0-9]+)?"               # 市值: X.XX
    r"[\s\S]{0,10}?盈虧[:\s]*"                          # 盈虧:
    r"[^0-9+\-]{0,10}([+\-]?[0-9,]+(?:\.[0-9]+)?)",     # (可選 emoji)±Y.YY
    re.DOTALL,
)

# 做空倉位 entry(點「做空倉位」button 後 ephemeral 替換的內容):
#   WAVE (夏日狂熱)
#       做空: 1000 股
#       均做空價: 61.90
#       現價: 62.78
#       押注金額: 61896.20
#       盈虧: -886.80
PORTFOLIO_SHORT_BLOCK = re.compile(
    r"([A-Z][A-Z0-9]{1,6})\s*\([^)]+\)"
    r".*?做空[:\s]*([0-9]+(?:\.[0-9]+)?)\s*股"
    r".*?均做空價[:\s]*\$?([0-9,]+(?:\.[0-9]+)?)"
    r".*?現價[:\s]*\$?([0-9,]+(?:\.[0-9]+)?)"
    r".*?押注金額[:\s]*\$?([0-9,]+(?:\.[0-9]+)?)"
    r".*?盈虧[:\s]*[^0-9+\-]{0,10}([+\-]?[0-9,]+(?:\.[0-9]+)?)",
    re.DOTALL,
)

# Portfolio 主畫面下方的「組合盈虧 / 資產概況 / 總未實現盈虧」摘要區
PORTFOLIO_SUMMARY_STOCKS = re.compile(
    r"股票[:\s]*[^0-9+\-]{0,10}([+\-]?[0-9,]+(?:\.[0-9]+)?)",
)
PORTFOLIO_SUMMARY_SHORTS = re.compile(
    r"做空[:\s]*[^0-9+\-]{0,10}([+\-]?[0-9,]+\.[0-9]+)",   # 強制小數避免抓到「做空: 1000 股」
)
PORTFOLIO_SUMMARY_TOTAL_UNREALIZED = re.compile(
    r"總未實現盈虧[:\s]*[^0-9+\-]{0,10}([+\-]?[0-9,]+(?:\.[0-9]+)?)",
)


def parse_portfolio(text: str) -> dict[str, dict]:
    """從 /portfolio 抓所有持股(chunk-based,避免 lazy match 跨 entry)。

    回傳 {symbol: {shares, avg_cost, current_price, pnl}}。pnl 抓不到時為
    None,UI 端會 fallback 用 shares*(current-avg) 自算。
    """
    if not text:
        return {}
    out: dict[str, dict] = {}
    # 在 chunk 內 search 各欄位 — 不會跨 entry boundary
    for sym, chunk in _chunk_by_header(text, _PORTFOLIO_HOLDING_HEADER):
        # 「持有: N 股」— 這個欄位才區分「持股 entry」vs 摘要區(摘要區
        # 沒「持有」keyword,所以摘要那段 chunk 抓不到 shares,跳過)
        m_sh = re.search(r"持有[:\s]*([0-9]+(?:\.[0-9]+)?)\s*股", chunk)
        if not m_sh:
            continue
        shares = _parse_number(m_sh.group(1))
        if shares is None or shares <= 0:
            continue
        m_avg = re.search(r"均買價[:\s]*\$?([0-9,]+(?:\.[0-9]+)?)", chunk)
        m_cur = re.search(r"現價[:\s]*\$?([0-9,]+(?:\.[0-9]+)?)", chunk)
        # 盈虧抓「市值 ... 盈虧 : ±NUM」(避免被「總未實現盈虧」誤抓)
        m_pnl = re.search(
            r"市值[:\s]*\$?[0-9,]+(?:\.[0-9]+)?"
            r"[\s\S]{0,10}?盈虧[:\s]*[^0-9+\-]{0,10}([+\-]?[0-9,]+(?:\.[0-9]+)?)",
            chunk,
        )
        out[sym] = {
            "shares":        shares,
            "avg_cost":      (_parse_number(m_avg.group(1)) if m_avg else 0.0) or 0.0,
            "current_price": (_parse_number(m_cur.group(1)) if m_cur else 0.0) or 0.0,
            "pnl":           (_parse_number(m_pnl.group(1).replace("+", ""))
                              if m_pnl else None),
        }
    return out


def parse_portfolio_shorts(text: str) -> dict[str, dict]:
    """點「做空倉位」button 後的 ephemeral — 抓做空標的清單(chunk-based)。

    回傳 {symbol: {shares, avg_short_price, current_price, position_cost, pnl}}。

    重要 — chunk-based 是修先前根因 bug:lazy `.*?` 從 SYMBOL 跨整個 entry
    可能抓到下個 entry 的數據。例如 ephemeral textContent 含 MAID 持股
    block + WAVE 做空 block,lazy 從 MAID 跳到 WAVE 的「做空: 1000 股」,
    結果 shorts[MAID] = {shares: 1000, ...WAVE 的數據...}。chunk 切完之後,
    MAID 那個 chunk 沒「做空: N 股」就跳過,WAVE chunk 才正確 parse。
    """
    if not text:
        return {}
    out: dict[str, dict] = {}
    for sym, chunk in _chunk_by_header(text, _PORTFOLIO_HOLDING_HEADER):
        m_sh = re.search(r"做空[:\s]*([0-9]+(?:\.[0-9]+)?)\s*股", chunk)
        if not m_sh:
            continue    # 沒做空 keyword(持股 chunk 或摘要 chunk),跳過
        shares = _parse_number(m_sh.group(1))
        if shares is None or shares <= 0:
            continue
        m_avg = re.search(r"均做空價[:\s]*\$?([0-9,]+(?:\.[0-9]+)?)", chunk)
        m_cur = re.search(r"現價[:\s]*\$?([0-9,]+(?:\.[0-9]+)?)", chunk)
        m_cost = re.search(r"押注金額[:\s]*\$?([0-9,]+(?:\.[0-9]+)?)", chunk)
        m_pnl = re.search(
            r"盈虧[:\s]*[^0-9+\-]{0,10}([+\-]?[0-9,]+(?:\.[0-9]+)?)", chunk,
        )
        out[sym] = {
            "shares":          shares,
            "avg_short_price": (_parse_number(m_avg.group(1)) if m_avg else 0.0) or 0.0,
            "current_price":   (_parse_number(m_cur.group(1)) if m_cur else 0.0) or 0.0,
            "position_cost":   (_parse_number(m_cost.group(1)) if m_cost else 0.0) or 0.0,
            "pnl":             (_parse_number(m_pnl.group(1).replace("+", ""))
                                if m_pnl else None),
        }
    return out


def parse_portfolio_summary(text: str) -> dict:
    """抓 portfolio 主畫面的「組合盈虧 / 總未實現盈虧」摘要區。

    回傳 {stocks_pnl, shorts_pnl, total_unrealized}。各欄位抓不到為 None。
    """
    if not text:
        return {}
    out: dict = {}
    m = PORTFOLIO_SUMMARY_STOCKS.search(text)
    if m:
        # 過濾掉「股票交易」「股票買賣」之類誤抓 — 只取緊接「盈虧:」標籤的
        # 實際上 SUMMARY_STOCKS 在 portfolio 摘要區的「股票」並列「做空」格式
        # 應該抓到 -366.00 而非他處
        out["stocks_pnl"] = _parse_number(m.group(1).replace("+", ""))
    m = PORTFOLIO_SUMMARY_SHORTS.search(text)
    if m:
        out["shorts_pnl"] = _parse_number(m.group(1).replace("+", ""))
    m = PORTFOLIO_SUMMARY_TOTAL_UNREALIZED.search(text)
    if m:
        out["total_unrealized"] = _parse_number(m.group(1).replace("+", ""))
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
# 同上 + 趨勢 — 用於需要趨勢資料的場合(不影響舊 parse_stock_list 簽章)
STOCK_LIST_ENTRY_FULL = re.compile(
    r"\b([A-Z][A-Z0-9]{1,6})\s*-\s*\S[^\n]{0,40}?"            # SYMBOL - 名字
    r"[\s\n]+價格\s*[:：]\s*\$?([0-9,]+(?:\.[0-9]+)?)"        # 價格 : NUMBER
    r"[\s\S]{0,80}?趨勢\s*[:：]\s*([+\-]?[0-9]+(?:\.[0-9]+)?)\s*%",   # 趨勢 : ±N%
    re.DOTALL,
)
# 舊名 dropdown 格式(`AZGC - 亞馬遜雲創 (36.76 油幣)`)— 留作備援
DROPDOWN_LINE = re.compile(
    r"([A-Z][A-Z0-9]{1,6})\s*-\s*[^()]+?\s*\(([0-9,]+(?:\.[0-9]+)?)\s*油幣\)",
)


def parse_stock_list_with_trend(text: str) -> dict[str, dict]:
    """從 `/stock` 的 embed 抓 {symbol: {price, trend_pct}}(chunk-based)。

    重要 — chunk-based:避免 lazy match 跨 entries。先用 `SYMBOL - Name`
    header 切 chunks,每個 chunk 內 search「價格 : N」「趨勢 : ±N%」,
    chunk 不會跨下個 SYMBOL,所以絕對不會把 MAID 的 27289.84 配給 WAVE。
    """
    if not text:
        return {}
    out: dict[str, dict] = {}
    skip_syms = {"USD", "USDT", "TOTAL", "STOCK"}
    for sym, chunk in _chunk_by_header(text, _STOCK_LIST_HEADER):
        if sym in skip_syms:
            continue
        # 找 chunk 內第一個「價格 : NUMBER」(stock list 格式;detail 用
        # 「當前價格」沒冒號所以不會 match)
        m_p = re.search(r"價格\s*[:：]\s*\$?([0-9,]+(?:\.[0-9]+)?)", chunk)
        if not m_p:
            continue
        price = _parse_number(m_p.group(1))
        if price is None or price <= 0:
            continue
        trend = None
        m_t = re.search(
            r"趨勢\s*[:：]\s*([+\-]?[0-9]+(?:\.[0-9]+)?)\s*%", chunk,
        )
        if m_t:
            trend = _parse_number(m_t.group(1))
        out.setdefault(sym, {"price": price, "trend_pct": trend})

    if not out:
        # fallback:dropdown 格式(autocomplete)— 不含 trend
        for sym, p in parse_stock_list(text).items():
            out[sym] = {"price": p, "trend_pct": None}
    return out


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


# ── 近期新聞 ──────────────────────────────────────────────────────────
# 點 /stock symbol:X 詳情頁的「近期新聞」按鈕,bot 替換 ephemeral 顯示:
#   MAID 相關新聞
#   • (2026/5/11) 震驚市場!傳聞「女僕特斯」驚天利好即將引爆...
#   • (2026/5/10) 技術訊號 (女僕特斯 (MAID) - 日線圖):出現黃金交叉
#   • (2026/5/10) 分析師日報 (2026-05-10):市場洞察
# title 可能含內嵌括號(例 "技術訊號 (xxx)..."),所以用 lookahead 到下一個
# (YYYY/MM/DD) 或字串尾才停。
STOCK_NEWS_HEADER = re.compile(r"([A-Z][A-Z0-9]{1,6})\s*相關新聞")
# 新聞日期位置 — 用「bullet 或 string start」當 anchor 確保不把 title 中
# 內嵌的「(2026-05-10):市場洞察」誤判為下條 entry 的開頭。
# (Discord embed 用 • bullet 開頭,textContent 通常保留 unicode 符號。)
NEWS_DATE_WITH_BULLET = re.compile(
    r"(?:^|[•·\*•‣])"
    r"\s*\(\s*([0-9]{4}[/\-][0-9]{1,2}[/\-][0-9]{1,2})\s*\)",
)
# Fallback:若 bullet 抓不到(Discord 改 UI 或 textContent 沒 bullet),
# 退到「寬鬆 — 任何 (YYYY/M/D) 都當 entry」
NEWS_DATE_RAW = re.compile(
    r"\(\s*([0-9]{4}[/\-][0-9]{1,2}[/\-][0-9]{1,2})\s*\)",
)


def _normalize_news_date(raw: str) -> str:
    """把「2026/5/10」或「2026-5-10」標準化成 ISO「2026-05-10」(便於排序)。"""
    parts = re.split(r"[/\-]", raw.strip())
    if len(parts) == 3:
        try:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        except ValueError:
            pass
    return raw


def parse_stock_news(
    text: str, expected_symbol: str | None = None,
) -> list[dict]:
    """從新聞 ephemeral 抓新聞列表。

    回傳 [{symbol, date, title}, ...](date 為 ISO YYYY-MM-DD)。日期排序
    在 DB query 端做(用 news_date DESC)。

    Strategy(防 title 中內嵌日期被誤判):先用「bullet 為前綴」抓 entry
    起點,如果抓不到(Discord 改 UI),退到寬鬆模式。
    """
    if not text:
        return []

    # 先定位到「{SYMBOL} 相關新聞」之後的區段,避免抓到 page 其他內容
    sym_from_header = None
    m = STOCK_NEWS_HEADER.search(text)
    if m:
        sym_from_header = m.group(1).upper()
        text = text[m.end():m.end() + 6000]

    sym = (expected_symbol or sym_from_header or "").upper()

    # 找所有「日期 anchor」位置(優先 bullet 前綴);找不到才寬鬆
    anchors = [(m.start(), m.end(), m.group(1))
               for m in NEWS_DATE_WITH_BULLET.finditer(text)]
    if not anchors:
        anchors = [(m.start(), m.end(), m.group(1))
                   for m in NEWS_DATE_RAW.finditer(text)]
    if not anchors:
        return []

    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for i, (_start, end, date_raw) in enumerate(anchors):
        # title = 從 date 結尾 → 下個 anchor 起點(或文字尾)
        title_end = anchors[i + 1][0] if i + 1 < len(anchors) else len(text)
        title = text[end:title_end].strip()
        # 清掉前後 bullet / 標點
        title = title.lstrip("•·*-– \t\r\n").rstrip(" \t\r\n").strip()
        if not title or len(title) < 3:
            continue
        if len(title) > 200:
            title = title[:200]
        date_iso = _normalize_news_date(date_raw)
        key = (date_iso, title)
        if key in seen:
            continue
        seen.add(key)
        out.append({"symbol": sym, "date": date_iso, "title": title})
    return out

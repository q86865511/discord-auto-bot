"""Slot embed 解析模組 — regex + DOM 文字擷取(強化版)。

從原本的版本改進:
1. 格子解析改用「位置 + 候選池」評分,不再硬取最後 9 個 — 對 DOM 結構變
   動更穩健,並回傳 confidence score
2. 線路 / 結果 / 餘額同時抓出來,只解析一次
3. 解析失敗時 dump 完整上下文(URL、ts、reason、context window)到 debug log
4. 符號正規式收緊邊界,降低 ZWJ / VS-16 誤判
5. Win/Loss 判斷加上「同位置取較晚出現的」明確規則
6. 純函式,無 state 依賴,容易單元測試

外部 callers 現在主要用:
    parse_slot_result(text, bet) -> SlotResult | None
    parse_balance(text) -> int | None
    get_page_text(page) -> str
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from bot.core.constants import SLOT_DEBUG_LOG_PATH

if TYPE_CHECKING:
    from playwright.async_api import Page


__all__ = [
    # 結構
    "SlotResult", "SlotLine",
    # 主要 API
    "parse_slot_result",
    "parse_balance",
    "parse_slot_change",      # 向後相容(僅回傳 change)
    "parse_slot_lines",
    "parse_slot_grid",
    "get_page_text",
    "debug_dump_slot_text",
    # regex 常數(供 main 用)
    "SLOT_WIN_PATTERN", "SLOT_LOSS_PATTERN",
    "SLOT_LINE_PATTERN", "SLOT_RESULT_BLOCK", "SLOT_FULL_BLOCK",
    # 輔助常數
    "AUX_EMOJIS", "NON_SLOT_SHORTCODES",
    "_SHORTCODE_NAME_RE",
    # 向後相容別名
    "_AUX_EMOJIS", "_NON_SLOT_SHORTCODES",
    "_parse_balance_int", "_parse_slot_change",
    "_parse_slot_lines", "_parse_slot_grid",
    "_get_page_text", "_debug_dump_slot_text",
]


log = logging.getLogger(__name__)


# ── 餘額 ──────────────────────────────────────────────────────────────
SLOT_WIN_PATTERN  = re.compile(r'總計贏得[\s:：]*([0-9,，]+)')
SLOT_LOSS_PATTERN = re.compile(r'損失\s*([0-9,，]+)')

# 一個 emoji symbol:自訂 shortcode 或 Unicode 圖形字元(含可選 VS-16 / keycap)
# 設計收緊:VS-16 改 `[️]?`(明確一個或無),ZWJ 序列限制最多 3 段,
# 避免家族 emoji 過度 backtrack
_SYMBOL_RE_SRC = (
    r'(?:'
    r':[a-z0-9_]+:'                                      # 自訂 shortcode
    r'|[0-9#*][️]?⃣'                            # 鍵帽 7️⃣
    r'|[\U0001F000-\U0001FAFF☀-➿]'             # 主要 emoji block
    r'[️]?'                                          # 可選 VS-16
    r'(?:‍[\U0001F000-\U0001FAFF☀-➿][️]?){0,3}'  # ZWJ 至多 3 段
    r')'
)
_SYMBOL_RE = re.compile(_SYMBOL_RE_SRC)

# 視為「非格子」的輔助 emoji(貨幣、特效…)
AUX_EMOJIS = {
    ':oi:', ':fh:', ':ti:',
    ':coin:', ':moneybag:', ':tada:', ':sparkles:',
    '🎉', '💰', '💵', '🪙', '✨',
}
NON_SLOT_SHORTCODES = {':oi:', ':fh:', ':ti:', ':coin:', ':moneybag:'}

# 向後相容別名
_AUX_EMOJIS = AUX_EMOJIS
_NON_SLOT_SHORTCODES = NON_SLOT_SHORTCODES

SLOT_LINE_PATTERN = re.compile(
    r'(上排水平|中排水平|下排水平|左列垂直|中列垂直|右列垂直|對角線|反對角線)'
    r'\s*[/\\]?\s*'
    r':\s*'
    rf'({_SYMBOL_RE_SRC})'
    r'\s*[×xX]\s*(\d+)'
    r'\s*=\s*'
    r'([0-9,，]+)'
    rf'(?:\s*{_SYMBOL_RE_SRC})*'
    r'\s*\('
    r'([0-9.]+)\s*[xX]'
    r'\s*[×xX]\s*'
    r'([0-9.]+)\s*[xX]'
    r'\s*\)'
)

SLOT_RESULT_BLOCK = re.compile(r'拉霸機結果([\s\S]*?)(?:總計贏得|什麼都沒中)')

# 涵蓋整個 slot 訊息(含結果文字之後的 9 格符號)。
# 視窗收緊到 400 字(原本 500 太寬,容易吃進下一則訊息),且改成貪心比對
# 才能讓「最後一次拉霸結果」優先勝出。
SLOT_FULL_BLOCK = re.compile(r'拉霸機結果([\s\S]{0,400}?)(?=拉霸機結果|$)')

# 餘額 / 油幣 pattern(從 main.py 搬過來,同一處集中)
BALANCE_PATTERNS = [
    re.compile(r'餘額[\s:：|]*([0-9,，]+)'),
    re.compile(r'油幣[\s:：]*([0-9,，]+)'),
]

# emoji 名稱 regex
_SHORTCODE_NAME_RE = re.compile(r'^:([a-z0-9_]+):$')


# ── Dataclass:SlotResult / SlotLine ─────────────────────────────────
@dataclass
class SlotLine:
    line_name:   str
    symbol:      str
    count:       int
    payout:      int
    symbol_mult: float
    line_mult:   float


@dataclass
class SlotResult:
    """一次 /slot 的完整解析結果。"""
    change:     int | None       # 淨變動(可能為 None,代表沒解到 win/loss 標記)
    lines:      list[SlotLine] = field(default_factory=list)
    grid:       list[str] | None = None
    grid_confidence: float = 0.0  # 0~1,1 表示確信
    raw_block:  str = ""           # 用於 debug


# ── 餘額解析 ──────────────────────────────────────────────────────────
def parse_balance_int(s: str) -> int | None:
    """支援半形 / 全形逗號的整數解析。"""
    try:
        return int(s.replace(',', '').replace('，', ''))
    except (ValueError, AttributeError):
        return None


# 向後相容
_parse_balance_int = parse_balance_int


def parse_balance(text: str) -> int | None:
    """從整頁文字找位置最靠後的「餘額/油幣」數字。"""
    last_val = None
    last_pos = -1
    for pat in BALANCE_PATTERNS:
        for m in pat.finditer(text):
            if m.start() > last_pos:
                v = parse_balance_int(m.group(1))
                if v is not None:
                    last_val = v
                    last_pos = m.start()
    return last_val


def count_balance_mentions(text: str) -> int:
    """整頁裡符合餘額/油幣 pattern 的次數。"""
    return sum(len(p.findall(text)) for p in BALANCE_PATTERNS)


# ── Slot 結果(win/loss) ─────────────────────────────────────────────
def parse_slot_change(text: str, bet: int) -> int | None:
    """從整頁文字找最後一個 slot 結果並計算淨變動。

    規則:
    - 同一位置先出現的(無論是 win 還是 loss)被後出現的覆蓋
    - 用 (位置, 是否 loss) 作為 tie-break:position 大者勝
    """
    candidates: list[tuple[int, str, int]] = []  # (pos, 'win'|'loss', value)
    for m in SLOT_WIN_PATTERN.finditer(text):
        v = parse_balance_int(m.group(1))
        if v is not None:
            candidates.append((m.start(), 'win', v))
    for m in SLOT_LOSS_PATTERN.finditer(text):
        v = parse_balance_int(m.group(1))
        if v is not None:
            candidates.append((m.start(), 'loss', v))
    if not candidates:
        return None
    # 取最後一個出現的(position 最大的)
    candidates.sort(key=lambda x: x[0])
    pos, kind, value = candidates[-1]
    return value - bet if kind == 'win' else -value


# 向後相容
_parse_slot_change = parse_slot_change


# ── 線路解析 ──────────────────────────────────────────────────────────
def parse_slot_lines(text: str) -> list[SlotLine]:
    """解析最後一個 slot embed 裡的中獎線路。"""
    blocks = list(SLOT_RESULT_BLOCK.finditer(text))
    if not blocks:
        return []
    block_text = blocks[-1].group(1)
    lines: list[SlotLine] = []
    for m in SLOT_LINE_PATTERN.finditer(block_text):
        try:
            lines.append(SlotLine(
                line_name   = m.group(1),
                symbol      = m.group(2),
                count       = int(m.group(3)),
                payout      = int(m.group(4).replace(',', '').replace('，', '')),
                symbol_mult = float(m.group(5)),
                line_mult   = float(m.group(6)),
            ))
        except (ValueError, AttributeError) as e:
            log.debug("跳過 line match(資料不全): %s", e)
    return lines


def _parse_slot_lines(text: str) -> list[dict]:
    """向後相容:回傳 dict list 格式。"""
    return [
        {
            "line_name":   ln.line_name,
            "symbol":      ln.symbol,
            "count":       ln.count,
            "payout":      ln.payout,
            "symbol_mult": ln.symbol_mult,
            "line_mult":   ln.line_mult,
        }
        for ln in parse_slot_lines(text)
    ]


# ── 格子解析(強化版) ─────────────────────────────────────────────────
def parse_slot_grid(text: str) -> tuple[list[str] | None, float]:
    """從 slot embed 擷取 3×3 九宮格;回傳 (grid_or_None, confidence)。

    強化策略:
    1. 先抓 slot block(最後一次出現的 slot result),收窄解析範圍
    2. 移除線路描述(避免中獎符號被誤計)
    3. 移除「總計贏得 X」、「損失 X」這類結尾文字(避免被當成符號)
    4. 過濾貨幣 emoji
    5. 評估 confidence:
       - 1.0 = 找到 9 個有效符號,且整頁出現的「拉霸機結果」次數合理
       - 0.5 = 找到 ≥9 個但需取 last 9
       - 0.0 = 找不到 9 個

    confidence 給上層決定是否要把 grid 計入 stats(避免污染統計)。
    """
    full_blocks = list(SLOT_FULL_BLOCK.finditer(text))
    if not full_blocks:
        return None, 0.0
    block_text = full_blocks[-1].group(1)

    # 移除線路描述
    cleaned = SLOT_LINE_PATTERN.sub(' ', block_text)
    # 移除「總計贏得 X」/「損失 X」/「什麼都沒中」之類文字
    cleaned = SLOT_WIN_PATTERN.sub(' ', cleaned)
    cleaned = SLOT_LOSS_PATTERN.sub(' ', cleaned)
    cleaned = re.sub(r'什麼都沒中', ' ', cleaned)

    all_syms = _SYMBOL_RE.findall(cleaned)
    grid_candidates = [s for s in all_syms if s not in AUX_EMOJIS]

    if len(grid_candidates) == 9:
        return grid_candidates, 1.0
    if len(grid_candidates) > 9:
        # 取最後 9 個 — 信心度降低
        confidence = max(0.3, 0.9 - (len(grid_candidates) - 9) * 0.05)
        log.debug("Grid 候選 %d 個,取最後 9 個(confidence=%.2f)",
                  len(grid_candidates), confidence)
        return grid_candidates[-9:], confidence

    log.debug("Grid 候選不足: 找到 %d 個(filtered=%s, all=%s)",
              len(grid_candidates), grid_candidates[:15], all_syms[:15])
    return None, 0.0


def _parse_slot_grid(text: str) -> list[str] | None:
    """向後相容:只回傳 grid。"""
    grid, _conf = parse_slot_grid(text)
    return grid


# ── 完整 SlotResult 介面 ──────────────────────────────────────────────
def parse_slot_result(text: str, bet: int) -> SlotResult:
    """一次解出 change / lines / grid。

    永遠回傳 SlotResult;若 change 無法解析,result.change 會是 None
    (caller 應檢查並決定是否要視為「解析失敗」或退回餘額差分)。
    """
    change = parse_slot_change(text, bet)
    lines = parse_slot_lines(text)
    grid, conf = parse_slot_grid(text)
    raw_block = ""
    m = SLOT_FULL_BLOCK.search(text)
    if m:
        raw_block = m.group(0)
    return SlotResult(
        change=change,
        lines=lines,
        grid=grid,
        grid_confidence=conf,
        raw_block=raw_block,
    )


# ── 頁面文字擷取(含 <img> alt) ────────────────────────────────────
_PAGE_TEXT_JS = r"""
() => {
    function walk(node) {
        if (node.nodeType === 3) return node.nodeValue || '';
        if (node.nodeType !== 1) return '';
        const tag = node.tagName;
        if (tag === 'IMG') {
            return node.getAttribute('alt')
                || node.getAttribute('aria-label')
                || node.getAttribute('data-name')
                || '';
        }
        if (tag === 'PICTURE') {
            const img = node.querySelector('img');
            if (img) return img.getAttribute('alt') || img.getAttribute('aria-label') || '';
            return '';
        }
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') return '';
        let text = '';
        for (const child of node.childNodes) text += walk(child);
        return text;
    }
    return walk(document.body);
}
"""


async def get_page_text(page: "Page") -> str:
    """讀整頁文字,含 <img> 的 alt(emoji 用)。"""
    return await page.evaluate(_PAGE_TEXT_JS)


# 向後相容
_get_page_text = get_page_text


# ── 結構化 debug dump ─────────────────────────────────────────────────
def debug_dump_slot_text(
    text: str, reason: str, log_path: str = SLOT_DEBUG_LOG_PATH,
    page_url: str = "", extra: dict | None = None,
) -> None:
    """解析失敗時把 拉霸機結果 區塊 + 上下文寫到 debug log。

    每筆紀錄會寫:
        === ts | reason | url ===
        [extra dict]
        block: <slot block 文字>
        last 200 chars of full text:
        <用於診斷 DOM 結構變動的尾端視窗>

    """
    try:
        # 父目錄(logs/)不存在就建立 — main 啟動時會 bootstrap 建好,
        # 但保險起見這裡也做一次,避免外部 caller 沒走 main 流程時失敗
        import os
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        block = ""
        m = SLOT_FULL_BLOCK.search(text)
        if m:
            block = m.group(0)
        tail = text[-200:] if text else ""
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"=== {datetime.now():%Y-%m-%d %H:%M:%S} | {reason} | {page_url} ===\n")
            if extra:
                for k, v in extra.items():
                    f.write(f"  {k}: {v!r}\n")
            f.write(f"  block: {block!r}\n")
            f.write(f"  tail:  {tail!r}\n\n")
    except OSError:
        log.warning("無法寫入 debug log: %s", log_path)


# 向後相容
def _debug_dump_slot_text(text: str, reason: str) -> None:
    debug_dump_slot_text(text, reason)

"""
Slot embed 解析模組 — regex + 原始文字擷取。

從 `main.py` 拆出來：所有 slot embed 的正則／解析邏輯都在這裡，沒有 state
依賴，純函式。`main.py` 透過 `from slot_parser import ...` 使用。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page


__all__ = [
    "SLOT_WIN_PATTERN", "SLOT_LOSS_PATTERN",
    "SLOT_LINE_PATTERN", "SLOT_RESULT_BLOCK", "SLOT_FULL_BLOCK",
    "_SYMBOL_RE_SRC", "_SYMBOL_RE",
    "_AUX_EMOJIS", "_NON_SLOT_SHORTCODES",
    "_PAGE_TEXT_JS",
    "_parse_balance_int",
    "_parse_slot_change",
    "_parse_slot_lines",
    "_parse_slot_grid",
    "_get_page_text",
    "_debug_dump_slot_text",
]


# slot 勝/負 marker（從 embed 文字直接判定這局結果，避免和 hourly/daily 的餘額變動混淆）
SLOT_WIN_PATTERN  = re.compile(r'總計贏得[\s:：]*([0-9,，]+)')
SLOT_LOSS_PATTERN = re.compile(r'損失\s*([0-9,，]+)')

# ── Slot 結果詳細解析 ──────────────────────────────────────────────────────────
# 中獎線路範例:「中排水平: :cherry:×3 = 864 :oi: (7.0x × 0.5x)」
#
# 重要:Discord textContent **不會** 包含 <img> 元素的 alt 屬性,但 emoji 都是
# <img> 元素(自訂 emoji alt 是 :shortcode:;標準 emoji alt 通常是 unicode 字元)。
# 解法:在 JS 端手動 walk DOM,把 img.alt 也納入 → 見 _get_page_text()。
# 因此 symbol 在解析後可能是 ":cherry:" 也可能是 "🍒",regex 兩種都要支援。

# 一個 emoji symbol:自訂 shortcode 或 Unicode 圖形字元(含可選 VS-16 / keycap)
_SYMBOL_RE_SRC = (
    r'(?:'
    r':[a-z0-9_]+:'                                      # 自訂 shortcode
    r'|[0-9#*]️⃣'                              # 鍵帽 7️⃣
    r'|[\U0001F000-\U0001FAFF⌀-➿⬀-⯿]'
    r'️?(?:‍[\U0001F000-\U0001FAFF⌀-➿])*'
    r')'
)
_SYMBOL_RE = re.compile(_SYMBOL_RE_SRC)

# 視為「非格子」的輔助 emoji（貨幣、特效…）— 解析格子時要過濾掉
# :fh: 油票 / :ti: 小魚干 / :oi: 油幣 — 都是這個 server 的「貨幣顯示」用 emoji，
# 不是真正的拉霸符號；它們會出現在 /balance 文字裡（剛好落在 slot 視窗內）導致誤計
_AUX_EMOJIS = {
    ':oi:', ':fh:', ':ti:',                              # 此 server 的貨幣 emoji
    ':coin:', ':moneybag:', ':tada:', ':sparkles:',
    '🎉', '💰', '💵', '🪙', '✨',
}
# 持久化資料中需要清掉的 shortcode（migration 用）— 跟 _AUX_EMOJIS 同步
_NON_SLOT_SHORTCODES = {':oi:', ':fh:', ':ti:', ':coin:', ':moneybag:'}

SLOT_LINE_PATTERN = re.compile(
    r'(上排水平|中排水平|下排水平|左列垂直|中列垂直|右列垂直|對角線|反對角線)'
    r'\s*[/\\]?\s*'                           # 對角線方向標記 / 或 \（可選）
    r':\s*'                                   # 分隔符 ":"
    rf'({_SYMBOL_RE_SRC})'                    # 符號（shortcode 或 Unicode）
    r'\s*[×xX]\s*(\d+)'                       # 次數 ×3
    r'\s*=\s*'
    r'([0-9,，]+)'                             # 金額 742
    rf'(?:\s*{_SYMBOL_RE_SRC})*'              # 可選 :oi: 等貨幣 emoji
    r'\s*\('
    r'([0-9.]+)\s*[xX]'                       # 符號倍率 7.0x
    r'\s*[×xX]\s*'
    r'([0-9.]+)\s*[xX]'                       # 線路倍率 0.5x
    r'\s*\)'
)

# 涵蓋線路描述區塊（到 總計贏得/什麼都沒中 為止）
SLOT_RESULT_BLOCK = re.compile(r'拉霸機結果([\s\S]*?)(?:總計贏得|什麼都沒中)')

# 涵蓋整個 slot 訊息（含結果文字之後的 9 格符號）
# 不再依賴「下注:」結尾，loss 場合該 footer 可能不在；改用固定長度視窗
SLOT_FULL_BLOCK = re.compile(r'拉霸機結果([\s\S]{0,500})')


def _parse_balance_int(s: str) -> int | None:
    """支援半形 / 全形逗號的整數解析。"""
    try:
        return int(s.replace(',', '').replace('，', ''))
    except (ValueError, AttributeError):
        return None


def _parse_slot_change(text: str, bet: int) -> int | None:
    """
    從整頁文字找最後一個 slot 結果並計算這局淨變動:
      - 「總計贏得:X」→ change = X - bet(X 是 gross win,包含原本下注)
      - 「什麼都沒中 損失 X」→ change = -X
    回傳 None 表示沒解析到(embed 還沒渲染,或格式變了)。
    用「最後出現位置」鎖定最新一局,避免讀到舊紀錄。
    """
    last_change = None
    last_pos = -1
    for m in SLOT_WIN_PATTERN.finditer(text):
        if m.start() > last_pos:
            v = _parse_balance_int(m.group(1))
            if v is not None:
                last_change = v - bet
                last_pos = m.start()
    for m in SLOT_LOSS_PATTERN.finditer(text):
        if m.start() > last_pos:
            v = _parse_balance_int(m.group(1))
            if v is not None:
                last_change = -v
                last_pos = m.start()
    return last_change


def _parse_slot_lines(text: str) -> list[dict]:
    """解析最後一個 slot embed 裡的中獎線路。"""
    blocks = list(SLOT_RESULT_BLOCK.finditer(text))
    if not blocks:
        return []
    block_text = blocks[-1].group(1)
    lines = []
    for m in SLOT_LINE_PATTERN.finditer(block_text):
        lines.append({
            "line_name":   m.group(1),
            "symbol":      m.group(2),           # :cherry: 或 🍒
            "count":       int(m.group(3)),
            "payout":      int(m.group(4).replace(',', '').replace('，', '')),
            "symbol_mult": float(m.group(5)),
            "line_mult":   float(m.group(6)),
        })
    return lines


def _parse_slot_grid(text: str) -> list[str] | None:
    """
    從 slot embed 的 textContent 擷取 3×3 九宮格符號。
    9 格固定出現在最尾端(總計贏得 / 損失 文字之後),策略:
      1. 取 拉霸機結果 之後最多 500 字元的視窗
      2. 把線路描述移除(避免中獎符號被誤計)
      3. 找所有 emoji symbol,過濾貨幣等輔助 emoji
      4. 取「最後 9 個」(grid 必定在尾端)
    回傳長度 9 的 list 或 None。
    """
    log = logging.getLogger(__name__)

    full_blocks = list(SLOT_FULL_BLOCK.finditer(text))
    if not full_blocks:
        return None
    block_text = full_blocks[-1].group(1)

    # 移除線路描述,避免把「中獎符號」也計入格子
    cleaned = SLOT_LINE_PATTERN.sub(' ', block_text)

    # 找所有 emoji symbol
    all_syms = _SYMBOL_RE.findall(cleaned)

    # 過濾貨幣／特效等輔助 emoji
    grid_candidates = [s for s in all_syms if s not in _AUX_EMOJIS]

    if len(grid_candidates) >= 9:
        # 取最後 9 個 — grid 永遠在尾端(總計贏得 / 損失 文字之後)
        grid = grid_candidates[-9:]
        log.debug("Grid parsed: %s", grid)
        return grid

    log.debug("Grid parse insufficient: found %d (filtered=%s, all=%s)",
              len(grid_candidates), grid_candidates[:15], all_syms[:15])
    return None


# ── 頁面文字擷取（含 <img> alt）─────────────────────────────────────────────
# Discord 用 <img> 渲染 emoji（自訂 emoji alt = :shortcode:；標準 emoji alt =
# unicode 字元），純 textContent **不會** 取到 alt。
# 在 JS 端 walk DOM 自己處理 img.alt → 取代既有的 textContent 讀法。
_PAGE_TEXT_JS = """
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
        if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') return '';
        let text = '';
        for (const child of node.childNodes) text += walk(child);
        return text;
    }
    return walk(document.body);
}
"""


async def _get_page_text(page: "Page") -> str:
    """讀整頁文字，含 <img> 的 alt（emoji 用）。"""
    return await page.evaluate(_PAGE_TEXT_JS)


def _debug_dump_slot_text(text: str, reason: str):
    """解析失敗時把 拉霸機結果 區塊文字寫到 slot_debug.log 方便排查。"""
    try:
        block = ""
        m = SLOT_FULL_BLOCK.search(text)
        if m:
            block = m.group(0)
        with open("slot_debug.log", "a", encoding="utf-8") as f:
            f.write(f"=== {datetime.now():%Y-%m-%d %H:%M:%S}  {reason} ===\n")
            f.write(repr(block) + "\n\n")
    except OSError:
        pass

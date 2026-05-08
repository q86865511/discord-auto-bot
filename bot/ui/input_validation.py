"""互動式輸入驗證 — 防止使用者輸入錯誤格式 / 中文字 / 異常範圍。

設計理念:
- 只認 ASCII 半形數字(避免使用者打全形 1, 而非 1)
- 範圍檢查:給 min / max 提示,輸入超出就拒絕
- 中文 / 全形 / 空白檢查:給 user-friendly 訊息
- 無效輸入 → 重問(最多 N 次,然後返回 None 表示「保留現值」)

主要 API:
    ask_int(prompt, current, min_val, max_val, allow_empty) → int | None
    ask_float(...)
    ask_choice(prompt, options, current) → str | None
    ask_yes_no(prompt, default) → bool
    ask_user_id(prompt, current) → str | None       Discord ID 純數字
    ask_text(prompt, current, max_len, allow_chinese) → str | None
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable

log = logging.getLogger(__name__)


_MAX_RETRIES = 3   # 連續輸入錯誤的最大次數,超過就 return None
_FULLWIDTH_DIGIT_MAP = str.maketrans(
    "012345678901234567890123456789",
    "012345678901234567890123456789",
)
_ASCII_DIGIT_MAP = str.maketrans(
    "0123456789  ,。",   # 全形數字 + 全形空白 + 全形逗號 + 全形句號
    "0123456789  ,.",
)


async def ainput(prompt: str) -> str:
    """async 包裝的 input()。"""
    return await asyncio.get_event_loop().run_in_executor(None, input, prompt)


def _normalize_input(s: str) -> str:
    """把全形空白 / 全形數字 / 全形逗號 normalize 成半形,並 strip。"""
    return s.translate(_ASCII_DIGIT_MAP).strip()


# ── 整數 ──────────────────────────────────────────────────────────────
async def ask_int(
    prompt: str,
    current: int | None = None,
    min_val: int | None = None,
    max_val: int | None = None,
    allow_negative: bool = True,
) -> int | None:
    """互動式詢問整數。

    - 空白(直接 Enter) → 回 None,代表保留現值
    - 中文 / 字母 / 含空白等無效 → 顯示警告並再問
    - 超出 [min_val, max_val] → 警告再問
    - 連續 _MAX_RETRIES 次失敗 → 回 None
    """
    cur_str = ""
    if current is not None:
        cur_str = f"目前 {current:,}" if current != 0 else "目前 0"

    range_hint = ""
    if min_val is not None and max_val is not None:
        range_hint = f"[{min_val:,}~{max_val:,}]"
    elif min_val is not None:
        range_hint = f"[≥{min_val:,}]"
    elif max_val is not None:
        range_hint = f"[≤{max_val:,}]"

    full_prompt = f"  {prompt} {range_hint} {cur_str}: ".strip().replace("  ", " ")

    for _ in range(_MAX_RETRIES):
        raw = (await ainput(full_prompt + " ")).strip()
        if not raw:
            return None
        norm = _normalize_input(raw)
        # 允許 -123 或 123,但不允許 "12 34" 或 "12abc"
        if not _looks_like_int(norm, allow_negative):
            print(f"  ⚠ 無效輸入「{raw}」(只允許半形數字{', 含負號' if allow_negative else ''}),請重新輸入")
            continue
        try:
            v = int(norm)
        except ValueError:
            print(f"  ⚠ 無法解析為整數「{raw}」")
            continue
        if min_val is not None and v < min_val:
            print(f"  ⚠ 數值不可小於 {min_val:,}")
            continue
        if max_val is not None and v > max_val:
            print(f"  ⚠ 數值不可大於 {max_val:,}")
            continue
        return v

    print(f"  ⚠ 連續 {_MAX_RETRIES} 次輸入無效,保留現值")
    return None


def _looks_like_int(s: str, allow_negative: bool) -> bool:
    if not s:
        return False
    if allow_negative and s.startswith("-"):
        s = s[1:]
    return s.isdigit()


# ── 浮點 ──────────────────────────────────────────────────────────────
async def ask_float(
    prompt: str,
    current: float | None = None,
    min_val: float | None = None,
    max_val: float | None = None,
) -> float | None:
    cur_str = f"目前 {current}" if current is not None else ""
    range_hint = ""
    if min_val is not None and max_val is not None:
        range_hint = f"[{min_val}~{max_val}]"
    elif min_val is not None:
        range_hint = f"[≥{min_val}]"
    elif max_val is not None:
        range_hint = f"[≤{max_val}]"

    full_prompt = f"  {prompt} {range_hint} {cur_str}: ".strip().replace("  ", " ")

    for _ in range(_MAX_RETRIES):
        raw = (await ainput(full_prompt + " ")).strip()
        if not raw:
            return None
        norm = _normalize_input(raw)
        try:
            v = float(norm)
        except ValueError:
            print(f"  ⚠ 無效輸入「{raw}」(必須是數字)")
            continue
        if v != v:    # NaN
            print("  ⚠ 不允許 NaN")
            continue
        if min_val is not None and v < min_val:
            print(f"  ⚠ 數值不可小於 {min_val}")
            continue
        if max_val is not None and v > max_val:
            print(f"  ⚠ 數值不可大於 {max_val}")
            continue
        return v

    print(f"  ⚠ 連續 {_MAX_RETRIES} 次輸入無效,保留現值")
    return None


# ── 選擇題(枚舉) ────────────────────────────────────────────────────
async def ask_choice(
    prompt: str, options: Iterable[str], current: str = "",
) -> str | None:
    options_list = [o.lower() for o in options]
    cur = f" [目前: {current}]" if current else ""

    for _ in range(_MAX_RETRIES):
        raw = (await ainput(f"  {prompt} ({'/'.join(options_list)}){cur}: ")).strip().lower()
        if not raw:
            return None
        if raw in options_list:
            return raw
        # 接受唯一前綴(例如 "p" 對應 "pause")
        matches = [o for o in options_list if o.startswith(raw)]
        if len(matches) == 1:
            return matches[0]
        print(f"  ⚠ 無效輸入,必須是 {'/'.join(options_list)} 之一")

    return None


# ── 是/否 ─────────────────────────────────────────────────────────────
async def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = "(Y/n)" if default else "(y/N)"
    raw = (await ainput(f"  {prompt} {suffix}: ")).strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes", "是", "好", "ok")


# ── Discord User ID(純數字 16~20 位) ────────────────────────────────
async def ask_user_id(prompt: str, current: str = "") -> str | None:
    cur = f"目前 {current[-6:] if current else '未設定'}" if current else ""

    for _ in range(_MAX_RETRIES):
        raw = (await ainput(f"  {prompt} {cur}: ")).strip()
        if not raw:
            return None
        norm = _normalize_input(raw)
        if not norm.isdigit():
            print("  ⚠ Discord ID 必須是純數字(目前輸入含非數字字元)")
            continue
        if not 15 <= len(norm) <= 22:
            print(f"  ⚠ Discord ID 長度通常是 17~19 位數字,你輸入 {len(norm)} 位")
            continue
        return norm

    return None


# ── 文字 ──────────────────────────────────────────────────────────────
async def ask_text(
    prompt: str, current: str = "", *,
    max_len: int = 200, allow_chinese: bool = True, allow_empty: bool = True,
) -> str | None:
    cur = f"目前 {current[:30]}{'...' if len(current) > 30 else ''}" if current else ""

    for _ in range(_MAX_RETRIES):
        raw = (await ainput(f"  {prompt} {cur}: "))
        # 不 strip,讓使用者可以保留前後空白(如有必要)。但去除尾巴的換行
        raw = raw.rstrip("\r\n")
        if not raw:
            if allow_empty:
                return ""    # 空字串
            return None      # 保留現值
        if len(raw) > max_len:
            print(f"  ⚠ 內容過長(>{max_len} 字),請重新輸入")
            continue
        if not allow_chinese and _has_chinese(raw):
            print("  ⚠ 此欄位不接受中文字元,請重新輸入")
            continue
        return raw

    return None


# ── 主機位址 ──────────────────────────────────────────────────────────
async def ask_host(prompt: str, current: str = "") -> str | None:
    """允許 0.0.0.0 / 127.0.0.1 / IPv4 / 'localhost'。"""
    cur = f"目前 {current}" if current else ""
    for _ in range(_MAX_RETRIES):
        raw = (await ainput(f"  {prompt} {cur}: ")).strip().lower()
        if not raw:
            return None
        if raw in ("0.0.0.0", "127.0.0.1", "localhost"):
            return raw
        if _looks_like_ipv4(raw):
            return raw
        print(f"  ⚠ 無效位址「{raw}」(可填 0.0.0.0 / 127.0.0.1 / IPv4)")
    return None


_CHINESE_RE = re.compile(r'[一-鿿]')

def _has_chinese(s: str) -> bool:
    return bool(_CHINESE_RE.search(s))


def _looks_like_ipv4(s: str) -> bool:
    parts = s.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit() or not 0 <= int(p) <= 255:
            return False
    return True


# ── 等待 Enter ────────────────────────────────────────────────────────
async def wait_enter(msg: str = "按 Enter 繼續...") -> None:
    await ainput(f"  {msg}")

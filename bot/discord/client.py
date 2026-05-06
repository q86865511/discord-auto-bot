"""Playwright 操作層 — 把所有對 Discord page 的指令 / 讀取包成 async API。

從原本的 main.py 拆出的部分:
- send_slash_command / send_message
- send_and_capture_balance / get_balance / play_slot
- navigate_to_channel / recover_page
- transfer 相關 helpers

所有「會送指令」的函式都會搶 `command_lock`,確保 hourly / daily / gambling
等多個 loop 的指令不會交錯解析。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING

from bot.core.constants import (
    RECOVER_PAGE_FAILS_BEFORE_BROWSER_RESTART,
    REPLY_WINDOW_CHARS,
    TYPING_DELAY_MAX_MS,
    TYPING_DELAY_MIN_MS,
)
from bot.core.state import BotState
from bot.slot.parsers import (
    count_balance_mentions,
    debug_dump_slot_text,
    get_page_text,
    parse_balance,
    parse_slot_result,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)


# 全域 lock — 所有送指令的動作共用,避免 hourly/daily/gambling 等 loop
# 在 Discord 端互相覆蓋訊息或誤讀回應
command_lock = asyncio.Lock()


# ── 基礎打字 / 送指令 ────────────────────────────────────────────────
async def human_type(page: "Page", text: str) -> None:
    """模擬真人打字,每個字元之間隨機 delay。"""
    for char in text:
        await page.keyboard.type(char)
        await asyncio.sleep(random.uniform(TYPING_DELAY_MIN_MS, TYPING_DELAY_MAX_MS) / 1000)


async def _send_slash_command(page: "Page", command: str, param: str = "") -> None:
    """實際送指令(不拿 lock,呼叫端必須先持有 command_lock)。"""
    log.info("準備送出指令: %s %s", command, param)
    input_box = page.locator('[data-slate-editor="true"]')
    await input_box.click()
    await asyncio.sleep(random.uniform(0.3, 0.8))
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)
    await human_type(page, command)
    await asyncio.sleep(1.5)
    await page.keyboard.press("Tab")
    await asyncio.sleep(random.uniform(0.4, 0.8))
    if param:
        await human_type(page, param)
        await asyncio.sleep(random.uniform(0.2, 0.5))
    await page.keyboard.press("Enter")
    await asyncio.sleep(random.uniform(0.5, 1.0))
    log.info("指令 %s %s 已送出", command, param)


async def send_slash_command(page: "Page", command: str, param: str = "") -> None:
    """公開的對外 API:會自動拿 command_lock。"""
    async with command_lock:
        await _send_slash_command(page, command, param)


async def _send_message(page: "Page", text: str) -> None:
    """送純文字訊息。呼叫端必須先持有 command_lock。

    用 keyboard.insert_text 一次插入,避免 `@` 觸發 mention autocomplete。
    """
    log.info("送出訊息: %s", text)
    input_box = page.locator('[data-slate-editor="true"]')
    await input_box.click()
    await asyncio.sleep(random.uniform(0.3, 0.6))
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)
    await page.keyboard.insert_text(text)
    await asyncio.sleep(0.5)
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.2)
    # Esc 可能也清掉輸入框內容;保險起見再 insert 一次後立刻 Enter
    try:
        current_text = await page.evaluate(
            "() => document.querySelector('[data-slate-editor=\"true\"]')?.textContent || ''"
        )
    except Exception:   # noqa: BLE001
        current_text = ""
    if text.strip() not in current_text:
        await page.keyboard.insert_text(text)
        await asyncio.sleep(0.3)
    await page.keyboard.press("Enter")
    await asyncio.sleep(0.5)


async def send_message(page: "Page", text: str) -> None:
    async with command_lock:
        await _send_message(page, text)


async def notify_goal_reached(page: "Page", balance: int, goal: int, user_id: str) -> None:
    """達標時 @ 使用者。"""
    msg = f"<@{user_id}> 已達成賭博目標!目前餘額 {balance:,} / 目標 {goal:,}"
    await send_message(page, msg)


# ── 餘額讀取 ──────────────────────────────────────────────────────────
def new_reply_detected(before_text: str, current_text: str) -> bool:
    """判斷自 before_text 之後是否有新「含餘額/油幣」回應出現。

    任一訊號成立即算 True:
      1. 整頁最後一個餘額數字變了
      2. 在最後 REPLY_WINDOW_CHARS 字視窗裡,餘額/油幣出現次數增加
    """
    bv = parse_balance(before_text)
    cv = parse_balance(current_text)
    if cv is not None and cv != bv:
        return True
    bt = before_text[-REPLY_WINDOW_CHARS:] if len(before_text) > REPLY_WINDOW_CHARS else before_text
    ct = current_text[-REPLY_WINDOW_CHARS:] if len(current_text) > REPLY_WINDOW_CHARS else current_text
    return count_balance_mentions(ct) > count_balance_mentions(bt)


async def read_initial_balance_from_history(page: "Page") -> int | None:
    """從已載入的聊天記錄找最近的餘額;找不到回 None。"""
    try:
        text: str = await page.evaluate("() => document.body.textContent")
    except Exception as e:  # noqa: BLE001
        log.warning("讀取頁面文字失敗: %s", e)
        return None
    return parse_balance(text)


async def send_and_capture_balance(
    page: "Page", command: str, param: str = "",
    timeout: float = 30.0, stability_sec: float = 2.0,
) -> int | None:
    """送指令並偵測新餘額。

    要求餘額值連續 `stability_sec` 秒不變才回傳,避免 slot bot 兩階段渲染
    (先扣下注、再加獎金)的中間狀態。
    """
    async with command_lock:
        try:
            before_text = await page.evaluate("() => document.body.textContent")
        except Exception as e:    # noqa: BLE001
            log.warning("讀取 before_text 失敗: %s", e)
            return None

        await _send_slash_command(page, command, param)

        deadline = time.time() + timeout
        last_val: int | None = None
        last_change_time: float = time.time()
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            try:
                current_text = await page.evaluate("() => document.body.textContent")
            except Exception as e:   # noqa: BLE001
                log.debug("輪詢讀文字失敗: %s", e)
                continue
            if not new_reply_detected(before_text, current_text):
                continue
            val = parse_balance(current_text)
            if val is None:
                continue
            if val != last_val:
                last_val = val
                last_change_time = time.time()
                continue
            if time.time() - last_change_time >= stability_sec:
                return val

        if last_val is not None:
            log.info("%s %s timeout 但已抓到值 %d,採用之", command, param, last_val)
            return last_val
        log.warning("%s %s 在 %.0fs 內未取得新餘額", command, param, timeout)
        return None


async def get_balance(page: "Page") -> int | None:
    """送 /balance 取得當前餘額。/balance 沒有動畫,1 秒穩定即可。"""
    return await send_and_capture_balance(page, "/balance", timeout=30.0, stability_sec=1.0)


async def play_slot(page: "Page", bet: int) -> dict | None:
    """送 /slot 並讀回新餘額 + 這局淨變動。

    回傳 {"balance": int, "change": int|None, "lines": [...], "grid": [...]} 或 None。

    /slot 動畫通常 2-3 秒,等 5 秒穩定才採用,避免抓到「先扣下注、未加獎金」
    的中間狀態。
    """
    timeout = 45.0
    stability_sec = 5.0

    async with command_lock:
        try:
            before_text = await get_page_text(page)
        except Exception as e:   # noqa: BLE001
            log.warning("讀取 before_text 失敗(slot): %s", e)
            return None

        await _send_slash_command(page, "/slot", param=str(bet))

        deadline = time.time() + timeout
        last_val: int | None = None
        last_change_time: float = time.time()
        last_text = before_text
        page_url = ""
        try:
            page_url = page.url or ""
        except Exception:    # noqa: BLE001
            pass

        while time.time() < deadline:
            await asyncio.sleep(0.5)
            try:
                current_text = await get_page_text(page)
            except Exception as e:   # noqa: BLE001
                log.debug("輪詢讀 slot 文字失敗: %s", e)
                continue
            last_text = current_text
            if not new_reply_detected(before_text, current_text):
                continue
            val = parse_balance(current_text)
            if val is None:
                continue
            if val != last_val:
                last_val = val
                last_change_time = time.time()
                continue
            if time.time() - last_change_time >= stability_sec:
                result = parse_slot_result(current_text, bet)
                _maybe_dump_debug(current_text, result, page_url, "stable")
                return _slot_result_to_dict(val, result)

        if last_val is not None:
            result = parse_slot_result(last_text, bet)
            _maybe_dump_debug(last_text, result, page_url, "timeout")
            log.info("/slot %d timeout 但已抓到值 %d (change=%s)",
                     bet, last_val, result.change)
            return _slot_result_to_dict(last_val, result)
        log.warning("/slot %d 在 %.0fs 內未取得新餘額", bet, timeout)
        return None


def _slot_result_to_dict(balance: int, result) -> dict:
    """把 SlotResult 轉成 main.py 期待的 dict 格式。"""
    return {
        "balance": balance,
        "change":  result.change,
        "lines":   [
            {
                "line_name":   ln.line_name,
                "symbol":      ln.symbol,
                "count":       ln.count,
                "payout":      ln.payout,
                "symbol_mult": ln.symbol_mult,
                "line_mult":   ln.line_mult,
            }
            for ln in result.lines
        ],
        "grid":            result.grid,
        "grid_confidence": result.grid_confidence,
    }


def _maybe_dump_debug(text: str, result, page_url: str, ctx: str) -> None:
    """解析有疑點時(贏但無線路、grid 完全沒解到)寫 debug log。"""
    if result.change is not None and result.change > 0 and not result.lines:
        debug_dump_slot_text(
            text, f"win but no lines parsed ({ctx})",
            page_url=page_url,
            extra={"change": result.change},
        )
    if result.grid is None:
        debug_dump_slot_text(
            text, f"grid not parsed ({ctx})",
            page_url=page_url,
        )


# ── 頻道 / Page 復原 ──────────────────────────────────────────────────
async def navigate_to_channel(page: "Page", guild_id: str, channel_id: str) -> None:
    url = f"https://discord.com/channels/{guild_id}/{channel_id}"
    log.info("導航至頻道: %s", url)
    await page.goto(url, wait_until="domcontentloaded")
    try:
        await page.wait_for_selector('[data-slate-editor="true"]', timeout=30_000)
    except Exception as e:
        try:
            current_url = page.url or ""
        except Exception:    # noqa: BLE001
            current_url = ""
        if "/login" in current_url or "/register" in current_url:
            msg = ("Discord session 已過期(storage_state.json 失效)。"
                   "請執行 login.bat 重新登入後再啟動 run.bat。")
        elif "discord.com/channels/" not in current_url:
            msg = (f"無法載入頻道頁,目前位置: {current_url}。"
                   "可能是 storage_state 過期、guild_id/channel_id 設定錯誤、"
                   "或網路連線問題。請檢查 config 的 ID 是否正確;"
                   "若仍然不行,執行 login.bat 重新登入。")
        else:
            msg = (f"頻道頁載入超過 30 秒仍找不到輸入框。網路太慢?Discord 改 UI?"
                   f"目前位置: {current_url}")
        log.error(msg)
        print()
        print("=" * 70)
        print(f"[啟動失敗] {msg}")
        print("=" * 70)
        raise RuntimeError(msg) from e
    log.info("頻道已載入")


async def recover_page(
    page: "Page", state: BotState, guild_id: str, channel_id: str,
) -> bool:
    """頁面變糟時重新載入頻道。連續失敗 N 次會請求整個 browser 重啟。"""
    if not guild_id or not channel_id:
        return False
    async with command_lock:
        log.warning("page 連續無回應,嘗試重新載入頻道...")
        try:
            await navigate_to_channel(page, guild_id, channel_id)
            await asyncio.sleep(3)
            log.info("頻道復原完成")
            async with state.lock:
                state.recover_fail_streak = 0
            return True
        except Exception as e:    # noqa: BLE001
            log.error("頻道復原失敗: %s", e)
            async with state.lock:
                state.recover_fail_streak += 1
                fails = state.recover_fail_streak
            if fails >= RECOVER_PAGE_FAILS_BEFORE_BROWSER_RESTART:
                log.error("recover_page 連續 %d 次失敗 → 觸發整個 browser 重啟", fails)
                state.queue_log(f"⚠ 連續 {fails} 次 recover 失敗,請求重啟 browser")
                async with state.lock:
                    state.reboot = True
                    state.quit = True
            return False


# ── 轉帳 ──────────────────────────────────────────────────────────────
async def _send_transfer_command(page: "Page", target: str, amount: int) -> None:
    """送 /transfer。呼叫端必須已持有 command_lock。"""
    input_box = page.locator('[data-slate-editor="true"]')
    await input_box.click()
    await asyncio.sleep(random.uniform(0.3, 0.8))
    await page.keyboard.press("Control+a")
    await page.keyboard.press("Backspace")
    await asyncio.sleep(0.2)

    await human_type(page, "/transfer")
    await asyncio.sleep(1.5)
    await page.keyboard.press("Tab")
    await asyncio.sleep(random.uniform(0.5, 0.9))

    await human_type(page, target)
    await asyncio.sleep(1.2)
    await page.keyboard.press("Enter")
    await asyncio.sleep(random.uniform(0.4, 0.8))

    await human_type(page, str(amount))
    await asyncio.sleep(random.uniform(0.3, 0.6))
    await page.keyboard.press("Enter")
    await asyncio.sleep(1.0)
    log.info("/transfer target=%s amount=%d 已送出", target, amount)


async def _click_button_with_text(page: "Page", text: str, timeout: float = 15.0) -> bool:
    """通用:等待並點擊含特定文字的按鈕。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            btn = page.locator(f'button:has-text("{text}")').last
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=3000)
                log.info("已點擊「%s」按鈕", text)
                return True
        except Exception as e:   # noqa: BLE001
            log.debug("等待「%s」按鈕中: %s", text, e)
        await asyncio.sleep(0.5)
    log.warning("等待「%s」按鈕超時 (%.0fs)", text, timeout)
    return False


async def do_transfer(page: "Page", target: str, amount: int) -> bool:
    """完整轉帳流程:送指令 + 點確認按鈕。"""
    async with command_lock:
        await _send_transfer_command(page, target, amount)
        return await _click_button_with_text(page, "確認轉錢", timeout=15.0)


# ── 貓娘 ──────────────────────────────────────────────────────────────
async def auto_claim_and_redispatch_neko(page: "Page") -> bool:
    """送 /nekomusume status → 等 ephemeral embed → 點「領取並再派遣」按鈕。

    呼叫端必須先持有 command_lock。
    """
    log.info("貓娘自動領取 + 再派遣 — 送 /nekomusume status")
    await _send_slash_command(page, "/nekomusume status", param="")
    await asyncio.sleep(2.5)
    ok = await _click_button_with_text(page, "領取並再派遣", timeout=15.0)
    if not ok:
        log.warning("找不到「領取並再派遣」按鈕 — 可能還在派遣中、或 button 文字變了")
    return ok


def parse_dispatch_status(text: str) -> tuple[str, int | None]:
    """從 /check 回應解析貓娘派遣狀態。

    回傳 (status, remaining_minutes):
        - "dispatching", N         派遣中,剩 N 分鐘
        - "not_dispatching", None  已完成或未派遣
        - "unknown", None          完全沒找到
    """
    import re
    m = re.search(r'貓娘派遣[\s\S]{0,80}?派遣中\s*(\d+)\s*(小時|分鐘)', text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        return "dispatching", (n * 60 if unit == "小時" else n)
    if "貓娘派遣" in text:
        return "not_dispatching", None
    return "unknown", None


async def read_check_response(page: "Page", timeout: float = 20.0) -> str | None:
    """送 /check 並等待新派遣資訊出現。回傳整頁文字。"""
    async with command_lock:
        try:
            before_text = await page.evaluate("() => document.body.textContent")
        except Exception as e:   # noqa: BLE001
            log.warning("讀 before_text 失敗(check): %s", e)
            return None
        before_st, before_min = parse_dispatch_status(before_text)
        await _send_slash_command(page, "/check")
        await asyncio.sleep(2.0)

        deadline = time.time() + timeout
        last_st, last_min, last_change = None, None, time.time()
        while time.time() < deadline:
            try:
                current = await page.evaluate("() => document.body.textContent")
            except Exception as e:   # noqa: BLE001
                log.debug("/check 輪詢讀文字失敗: %s", e)
                await asyncio.sleep(0.5)
                continue
            cur_st, cur_min = parse_dispatch_status(current)
            if (cur_st, cur_min) != (before_st, before_min):
                if (cur_st, cur_min) == (last_st, last_min):
                    if time.time() - last_change >= 1.0:
                        return current
                else:
                    last_st, last_min = cur_st, cur_min
                    last_change = time.time()
            await asyncio.sleep(0.5)

        log.info("/check 未偵測到狀態變化,回傳當前整頁文字")
        try:
            return await page.evaluate("() => document.body.textContent")
        except Exception as e:   # noqa: BLE001
            log.warning("讀 fallback page text 失敗: %s", e)
            return None

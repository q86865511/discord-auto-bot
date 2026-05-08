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
            msg = ("Discord session 已過期(data/storage_state.json 失效)。"
                   "重啟 run.bat — 程式會自動引導你重新登入。")
        elif "discord.com/channels/" not in current_url:
            msg = (f"無法載入頻道頁,目前位置: {current_url}。"
                   "可能是 storage_state 過期、guild_id/channel_id 設定錯誤、"
                   "或網路連線問題。請檢查設定 ID 是否正確;"
                   "若仍然不行,刪掉 data/storage_state.json 後重啟 run.bat 重新登入。")
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


# ── 股票 ──────────────────────────────────────────────────────────────
def _stock_reply_detected(before: str, current: str) -> bool:
    """偵測 /stock 或 /portfolio 是否回了新訊息。

    `new_reply_detected` 是給 /balance / /slot 用的(看餘額變化),不適合
    /stock list(只有價格,沒有 user 餘額更新)。

    偵測邏輯:檢查 stock 相關關鍵字在「最近的回應視窗」(尾巴 4000 字)裡
    出現次數有沒有增加。任一個變多就算有新回應。
    """
    keywords = ("股市行情", "投資組合", "持有:", "持有：",
                "價格 :", "價格：", "現價:", "現價：")
    win = 4000
    bt = before[-win:]
    ct = current[-win:]
    for kw in keywords:
        if ct.count(kw) > bt.count(kw):
            return True
    return False


async def _try_click(page: "Page", text: str, timeout_ms: int = 5000) -> bool:
    """嘗試用多種方式點到含 `text` 的可點元素。

    Discord embed 裡的按鈕、modal dropdown 選項、確認按鈕格式不同,
    試:role=button → role=option → role=menuitem → 任意 text。
    """
    selectors = [
        ("button by role+name", lambda: page.get_by_role("button", name=text).first),
        ("option by role+name", lambda: page.get_by_role("option", name=text).first),
        ("menuitem by role+name",
            lambda: page.get_by_role("menuitemradio", name=text).first),
        ("text exact", lambda: page.get_by_text(text, exact=True).first),
        ("text contains", lambda: page.get_by_text(text).first),
    ]
    for label, mk in selectors:
        try:
            loc = mk()
            await loc.click(timeout=timeout_ms)
            log.debug("trade: 點到 '%s' via %s", text, label)
            return True
        except Exception as e:    # noqa: BLE001
            log.debug("trade: 點 '%s' via %s 失敗: %s", text, label, e)
    return False


async def execute_stock_trade(
    page: "Page", action: str, symbol: str, amount: int,
    open_button: str = "操作股票",
    buy_option: str = "買入股票",
    sell_option: str = "賣出股票",
    submit_button: str = "提交",
    confirm_button: str = "確認",
    stock_command: str = "/stock",
    timeout: float = 60.0,
) -> tuple[bool, str]:
    """執行買賣交易(button-based,米米警察 bot 流程)。

    步驟:
      1. 送 /stock symbol:X → embed 出現
      2. 點 [操作股票] → modal 開啟
      3. modal 下拉選 [買入股票] / [賣出股票]
      4. 填入股數
      5. 點 [提交]
      6. 確認 embed 出現 → 點 [確認]
      7. 看結果:操作成功 / 失敗

    回傳 (success, message)。
    """
    if action not in ("buy", "sell"):
        return False, f"未知 action: {action}"

    log.info("trade exec: %s %s × %d (button-based)", action, symbol, amount)
    option_label = buy_option if action == "buy" else sell_option

    async with command_lock:
        # ── Step 1: /stock symbol:X ─────────────────────────────────
        try:
            await _send_slash_command(page, stock_command, f"symbol:{symbol}")
        except Exception as e:    # noqa: BLE001
            return False, f"step 1 送 /stock 失敗: {e}"

        # 等 embed 渲染
        await asyncio.sleep(3.0)
        log.info("trade step 1: /stock symbol:%s sent", symbol)

        # ── Step 2: 點「操作股票」 ───────────────────────────────────
        if not await _try_click(page, open_button, timeout_ms=10000):
            return False, f"step 2 找不到「{open_button}」按鈕(/stock 沒回應?)"
        log.info("trade step 2: 點到 '%s'", open_button)
        await asyncio.sleep(1.8)    # 等 modal 渲染

        # ── Step 3: 在 modal 下拉選買/賣 ────────────────────────────
        # 有些 bot 的 dropdown 已展開,有些要先點 trigger
        # 先試直接點 option;失敗就點 combobox 展開後再試
        clicked_option = await _try_click(page, option_label, timeout_ms=2500)
        if not clicked_option:
            # 嘗試點 dialog 內的 combobox 展開
            try:
                combo = page.locator('[role="dialog"] [role="combobox"]').first
                if await combo.count() > 0:
                    await combo.click(timeout=3000)
                    await asyncio.sleep(0.6)
                    log.debug("trade: 展開 combobox")
            except Exception as e:    # noqa: BLE001
                log.debug("trade: combobox 展開失敗(可能不存在): %s", e)
            # 再試一次點選項
            clicked_option = await _try_click(page, option_label, timeout_ms=8000)
        if not clicked_option:
            return False, f"step 3 找不到下拉選項「{option_label}」"
        log.info("trade step 3: 選到 '%s'", option_label)
        await asyncio.sleep(1.2)

        # ── Step 4: 填股數 ──────────────────────────────────────────
        try:
            # modal 內第一個 input(通常就是股數)
            inp = page.locator('[role="dialog"] input').first
            if await inp.count() == 0:
                # fallback: 全頁找
                inp = page.locator('input[type="number"], input').first
            await inp.fill(str(int(amount)), timeout=5000)
            log.info("trade step 4: 填 amount=%d", amount)
        except Exception as e:    # noqa: BLE001
            return False, f"step 4 填股數失敗: {e}"
        await asyncio.sleep(0.6)

        # ── Step 5: 點提交 ──────────────────────────────────────────
        if not await _try_click(page, submit_button, timeout_ms=8000):
            # 嘗試 modal 內的 type=submit 按鈕
            try:
                btn = page.locator('[role="dialog"] button[type="submit"]').first
                if await btn.count() > 0:
                    await btn.click(timeout=3000)
                    log.debug("trade: 用 button[type=submit] fallback")
                else:
                    return False, f"step 5 找不到「{submit_button}」提交按鈕"
            except Exception as e:    # noqa: BLE001
                return False, f"step 5 提交失敗: {e}"
        log.info("trade step 5: 提交 modal")
        await asyncio.sleep(2.5)    # 等確認 embed 出現

        # ── Step 6: 點確認 embed 上的「確認」 ────────────────────────
        if not await _try_click(page, confirm_button, timeout_ms=10000):
            return False, f"step 6 找不到「{confirm_button}」確認按鈕"
        log.info("trade step 6: 點「%s」確認", confirm_button)
        await asyncio.sleep(2.5)    # 等結果

        # ── Step 7: 讀結果 ──────────────────────────────────────────
        try:
            result = await page.evaluate("() => document.body.textContent")
            tail = result[-2500:]
        except Exception as e:    # noqa: BLE001
            return False, f"step 7 讀結果失敗(動作可能已執行): {e}"

        if any(k in tail for k in ("操作成功", "成功購買", "成功賣出", "成功買入")):
            return True, f"✅ {action} {symbol} × {amount} 成功"
        if any(k in tail for k in ("交易失敗", "餘額不足", "找不到股票", "已過期",
                                     "股數不足", "限制交易")):
            for k in ("餘額不足", "股數不足", "找不到股票", "限制交易",
                      "交易失敗", "已過期"):
                if k in tail:
                    return False, f"❌ bot 回報:{k}"
            return False, "❌ bot 回報失敗(看 logs/trading.log)"

        # 沒明確訊號 — 動作可能已執行,但無法確認
        return True, (f"⚠ {action} {symbol} × {amount} 已送出,"
                      f"但讀不到明確成功/失敗訊號(看 /portfolio 確認)")


async def query_stock_text(
    page: "Page", command: str = "/stock", param: str = "",
    timeout: float = 20.0, stability_sec: float = 1.5,
) -> str | None:
    """送 stock 查詢指令並回傳整頁文字。caller 自行 parse。

    `command` 可以是 /stock / /portfolio 等;`param` 是參數(可空)。
    """
    async with command_lock:
        try:
            before_text = await page.evaluate("() => document.body.textContent")
        except Exception as e:    # noqa: BLE001
            log.warning("讀取 before_text 失敗(stock): %s", e)
            return None

        await _send_slash_command(page, command, param)

        deadline = time.time() + timeout
        last_text = None
        last_change = time.time()
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            try:
                cur = await page.evaluate("() => document.body.textContent")
            except Exception as e:    # noqa: BLE001
                log.debug("stock 輪詢失敗: %s", e)
                continue
            if not _stock_reply_detected(before_text, cur):
                continue
            if cur != last_text:
                last_text = cur
                last_change = time.time()
                continue
            if time.time() - last_change >= stability_sec:
                return cur
        if last_text is not None:
            return last_text
        log.warning("%s %s 在 %.0fs 內未取得新回應", command, param, timeout)
        return None


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

"""
第一次執行此腳本以儲存 Discord 登入狀態。
會開啟真實瀏覽器視窗，請手動登入後等待自動關閉。
"""
import asyncio
from playwright.async_api import async_playwright

STORAGE_STATE_PATH = "storage_state.json"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        print("開啟 Discord 登入頁面，請手動完成登入...")
        await page.goto("https://discord.com/login")

        # 等待登入完成（URL 變成 /channels/...）
        await page.wait_for_url("**/channels/**", timeout=120_000)
        print("登入成功！儲存 session 中...")

        await context.storage_state(path=STORAGE_STATE_PATH)
        await browser.close()

        print(f"完成！已儲存至 {STORAGE_STATE_PATH}")
        print("往後直接執行 main.py 即可，不需重新登入。")


if __name__ == "__main__":
    asyncio.run(main())

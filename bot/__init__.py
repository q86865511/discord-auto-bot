"""Discord Auto Bot — 模組化 package(v2 — 全面重構)。

子套件:
- bot.core           DB(SQLite)/ 加密(Fernet)/ 設定 schema(dataclasses)
                     / 狀態管理(asyncio.Lock)/ async I/O / log redaction
- bot.discord        Playwright wrapper(送指令、讀餘額、play_slot、轉帳)
- bot.scheduler      5 個排程 loop(hourly / daily / gambling / transfer
                     / nekomusume / digest)
- bot.notifications  email 通知 + 達標 / 停損 / 中大獎 / 停擺判斷
- bot.slot           slot embed 解析(parsers)+ 累計分析 + Kelly 計算
- bot.ui             Rich 終端 UI + 互動式設定選單(含輸入驗證)
- bot.web            Web dashboard(http.server + auth + CSRF + log redact)

main.py 是 entry point,只負責:
1. 載入加密金鑰 + 初始化 DB
2. 一次性遷移舊 JSON 設定 / 分析 / 歷史
3. 跑首次設定 wizard(若有缺欄位 / dashboard 無密碼)
4. 啟動 Playwright + 啟動所有 scheduler tasks + ui_loop
5. Graceful shutdown + 重啟邏輯

設計原則:
- 所有持久化資料(設定、歷史、分析)走 SQLite,敏感欄位(密碼)加密
- 所有 state 修改走 asyncio.Lock(避免 race condition)
- 所有同步 I/O(檔案、subprocess、SMTP)用 asyncio.to_thread 包裝
- 異常處理具體化(避免 except Exception: pass 吞掉錯誤)
- 設定走 dataclass + validate(),wizard / dashboard 都會跑驗證
"""

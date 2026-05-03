# Discord Auto Bot

以 Playwright 控制 Chromium 自動執行 Discord 斜線指令的腳本，內建 Rich 終端 UI。

主要功能：
- 自動定期執行 `/hourly`、`/daily`
- 自動 `/slot` 賭博，含可調策略（auto / fixed）、保底門檻、上下注上限、間距
- 餘額目標達成時 `@` 指定使用者
- 即時統計：總下注、勝率、淨收
- 一鍵暫停 / 恢復、匯出賭博紀錄（CSV + 餘額曲線圖）

> **使用前須知**：本腳本透過真實瀏覽器自動化操作 Discord。自動化使用者帳號違反 Discord 服務條款，使用造成的帳號處置請自行承擔。僅供研究、學習用途。

## 系統需求

- Windows 10 / 11
- Python 3.10+（已加入 PATH）

## 安裝

雙擊執行：

```
setup.bat
```

會自動建 venv、裝 `playwright` / `rich` / `matplotlib`、下載 Chromium，並從 `config.example.json` 複製一份 `config.json`。

接著編輯 `config.json` 填入：

| 欄位 | 說明 | 怎麼取得 |
|------|------|---------|
| `guild_id` | 伺服器 ID | Discord 開發者模式 → 對伺服器右鍵「複製伺服器 ID」 |
| `channel_id` | 要操作的頻道 ID | 對頻道右鍵「複製頻道 ID」 |
| `gambling.notify_user_id` | 達標時要 @ 的人的 User ID | 對使用者右鍵「複製使用者 ID」 |

（開啟開發者模式：使用者設定 → 進階 → 啟用「開發者模式」）

## 登入 Discord

雙擊執行：

```
login.bat
```

會跳出 Chromium 視窗，**請手動完成 Discord 登入**（含 2FA 等）。當網址跳轉到 `/channels/...` 時會自動關閉並儲存 `storage_state.json`。往後不需要再登入。

## 啟動

雙擊執行：

```
run.bat
```

UI 上的快速鍵：

| 鍵 | 功能 |
|----|------|
| `Q` | 退出 |
| `C` | 修改設定（互動式選單）|
| `R` | 從檔案重載設定 |
| `P` | 暫停 / 恢復所有功能（會即時打斷 hourly/daily 等待）|
| `E` | 匯出賭博紀錄為 CSV + PNG 圖表（存到 `exports/`）|

## 賭博設定說明

`config.json` → `gambling` 內的欄位：

| 欄位 | 預設 | 說明 |
|------|------|------|
| `enabled` | `false` | 是否啟用自動賭博 |
| `threshold` | `5000` | 保底門檻：餘額低於這個值就停止下注 |
| `min_bet` | `100` | 單次最小下注 |
| `max_bet` | `500` | 單次最大下注（`0` = 自動：餘額超過門檻部分的 10%）|
| `strategy` | `"auto"` | `auto` = 按比例；`fixed` = 固定 `min_bet` |
| `bet_fraction` | `0.02` | auto 策略：押注超過門檻部分的這個比例 |
| `interval_min` | `4` | 兩次下注之間最短秒數 |
| `interval_max` | `10` | 兩次下注之間最長秒數 |
| `goal` | `0` | 餘額目標；達到時 `@` 指定使用者，`0` = 不啟用 |
| `notify_user_id` | — | 目標達成時要通知的人的 Discord User ID |

## 檔案說明

```
.
├── main.py                  主程式（UI + 自動化迴圈）
├── login.py                 第一次登入用
├── setup.bat                安裝環境（venv + 套件 + Chromium）
├── login.bat                執行登入
├── run.bat                  啟動 bot
├── requirements.txt         Python 套件清單
├── config.example.json      設定檔範本
├── config.json              你的實際設定（已被 .gitignore，不會上傳）
├── storage_state.json       Discord session（已被 .gitignore，不會上傳）
└── exports/                 匯出的賭博紀錄（已被 .gitignore）
```

## 技術說明

- **餘額讀取**：用 `body.textContent` 比對「餘額/油幣」字樣的出現次數差，比 `chat-messages-` count diff 更穩
- **Slot 動畫處理**：要求餘額值連續 5 秒不變才採用，避免讀到 bot「先扣下注、再加獎金」的中間狀態（會把贏的場次誤判為輸）
- **指令序列化**：所有送指令的動作共用 `asyncio.Lock`，避免 hourly / daily / gambling 三個 loop 互相污染對方的回應解析
- **暫停機制**：所有長 sleep 都用 0.5 秒分段，可被「P 鍵」即時打斷

## 常見問題

**Q: 餘額一直讀不到？**
A: 確認頻道權限正常、bot 有回應使用者、`/balance` 指令可用。看 UI 日誌欄是否顯示 timeout。

**Q: 想換頻道？**
A: 改 `config.json` 的 `channel_id`，按 `R` 重載即可，不用重啟。

**Q: 可以同時跑多個帳號嗎？**
A: 開多個資料夾分別放各自的 `config.json` / `storage_state.json` 即可。

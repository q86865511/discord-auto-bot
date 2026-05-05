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
| `C` | 修改設定（互動式選單；存檔後即時套用，不需手動重載）|
| `P` | 暫停 / 恢復所有功能（會即時打斷 hourly/daily 等待）|
| `E` | 匯出賭博紀錄為 CSV + PNG 圖表 + Slot 分析報告（存到 `exports/`）|
| `S` | 查看 Slot 分析報告（EV、符號統計、線路統計、Kelly 建議、ASCII 賭博紀錄圖）|
| `L` | 重新載入 Discord 頻道頁面（page state 變糟時用）|
| `F` | 整個程式重啟（透過 `run.bat` loop 達成；直接 `python main.py` 跑時會直接退出）|

## 賭博設定說明

`config.json` → `gambling` 內的欄位：

| 欄位 | 預設 | 說明 |
|------|------|------|
| `enabled` | `false` | 是否啟用自動賭博 |
| `threshold` | `5000` | 保底門檻：餘額低於這個值就停止下注 |
| `min_bet` | `100` | 單次最小下注 |
| `max_bet` | `500` | 單次最大下注（`0` = 自動：餘額超過門檻部分的 10%）|
| `strategy` | `"auto"` | `auto` = 按比例；`fixed` = 固定 `min_bet`；`kelly` = 依據期望值的 Kelly Criterion（詳見下方說明）|
| `bet_fraction` | `0.02` | auto 策略：押注超過門檻部分的這個比例 |
| `interval_min` | `4` | 兩次下注之間最短秒數 |
| `interval_max` | `10` | 兩次下注之間最長秒數 |
| `goal` | `0` | 餘額目標；達到時通知，`0` = 不啟用 |
| `goal_action` | `pause` | 達標後動作：`pause` = 停用賭博；`raise` = 把已達目標當作新門檻、目標 += `goal_step` 後繼續（保護獲利的階梯式策略）|
| `goal_step` | `10000` | `raise` 模式下，新目標 = 舊目標 + 此值 |
| `loss_floor` | `0` | 停損點：餘額跌到這個值就觸發停損動作，`0` = 不啟用 |
| `loss_action` | `pause` | 觸發停損後動作：`pause` = 停用賭博；`lower_threshold` = 把門檻拉到「當前餘額 - `loss_step`」、停損點同步下移後繼續（階梯式停損）|
| `loss_step` | `5000` | `lower_threshold` 模式下，新門檻 = 餘額 - 此值；新停損點 = 舊停損點 - 此值 |
| `bigwin_multiplier` | `5.0` | 中大獎賠率門檻（總計贏得 / 下注 ≥ 此值就寄 email；需 `email.notify_bigwin` 啟用）|
| `notify_user_id` | — | 目標達成 / 貓娘完成時要 `@` 的 Discord User ID |

> **停損 vs 保底門檻**：
> - `threshold`（保底）= 餘額低於此就「等待」（不下注、不通知，只是觀望）
> - `loss_floor`（停損）= 餘額低於此就「觸發動作」（停用 bot 或下移門檻 + 寄信）
> - 通常 `loss_floor < threshold` 才合理（先停下注，跌得更慘才停損）

### Email 通知（選用）

`config.json` → `email`：

| 欄位 | 預設 | 說明 |
|------|------|------|
| `enabled` | `false` | 主開關，整個 email 功能 |
| `smtp_host` / `smtp_port` | Gmail | SMTP 伺服器 |
| `user` | — | 寄件人帳號 |
| `password` | — | **Gmail 必須用 [App Password](https://myaccount.google.com/apppasswords)，不是登入密碼** |
| `to` | — | 收件人 |
| `notify_goal` | `true` | 達成 `goal` 時寄信 |
| `notify_loss` | `true` | 觸發 `loss_floor` 停損時寄信 |
| `notify_bigwin` | `true` | /slot 中大獎時寄信（賠率 ≥ `gambling.bigwin_multiplier`）|
| `notify_dead` | `true` | 連續無法讀取餘額（/slot + /balance）時寄信，提醒 bot 可能掛了 |
| `notify_neko` | `true` | 貓娘派遣完成時寄信 |
| `notify_digest` | `true` | 每日 `digest_hour` 整點寄一次 24h 摘要（總下注、勝率、淨收、EV、各事件次數） |
| `digest_hour` | `0` | 每日摘要的觸發時段（0~23 整點；預設 00:00） |
| `dead_threshold` | `2` | 連續失敗達此次數就視為「bot 停擺」並寄一次警告（每段死亡只寄一次，恢復後重置）|

六種事件：
- **達成目標**：餘額 ≥ `goal`
- **觸發停損**：餘額 ≤ `loss_floor`（每段下跌只寄一次，回升後重置）
- **中大獎**：/slot 結果「總計贏得 / 下注」≥ `bigwin_multiplier`（預設 5x）
- **bot 停擺**：/slot 或 /balance 連續失敗 ≥ `dead_threshold`
- **每日摘要**：每天 `digest_hour` 整點，只要那一小時內醒過來且沒寄過，就會寄一次
- **貓娘完成**：派遣狀態從「派遣中」變回「閒置 / 待領取」

### 貓娘派遣監控

`config.json` → `nekomusume`：

| 欄位 | 預設 | 說明 |
|------|------|------|
| `enabled` | `true` | 是否監控 |
| `check_interval_min` | `30` | 基本檢查間距（分鐘）；剩 1 小時內會自動加密輪詢 |

每隔 N 分鐘送一次 `/check`（ephemeral，不會洗版），偵測到從「派遣中」轉為「完成 / 閒置」時，自動 `@` 指定使用者，提醒 `/nekomusume claim` 領取。同時若 email 啟用也會寄信。

### Slot 分析與 Kelly 策略

按 `S` 鍵可查看完整分析報告，包含：

- **期望值 (EV)**：每次下注的平均回報倍率。EV > 1 = 機器對玩家有利；EV < 1 = 莊家佔優
- **符號統計**：各符號的中獎次數、平均倍率、總賠付，以及從九宮格解析的出現機率
- **線路統計**：各連線方向的命中次數與命中率
- **賠率分布**：0x / 0-1x / 1-2x / 2-5x / 5-10x / 10x+ 的分布直方圖
- **Kelly Criterion**：基於 EV 和變異數計算的最佳下注比例

**Kelly 策略** (`strategy: "kelly"`)：

- 需要累計 50 筆以上轉數才會啟用；資料不足時以 `min_bet` 下注
- 使用半 Kelly（f*/2）以降低波動風險
- EV ≤ 1（負期望值）時固定下 `min_bet`，不停止賭博，UI 會清楚顯示 EV
- 可隨時從設定選單切回 `auto` 或 `fixed`

分析資料持久化在 `slot_analysis.json`，重啟後會自動載入繼續累計。可在設定選單 `[I]` 重置。

下注歷史紀錄持久化在 `gambling_history.json`（最近 5000 筆），重啟後仍可從 `S` 鍵看到 ASCII 賭博紀錄圖、按 `E` 鍵匯出 CSV / PNG 折線圖。

### 自動轉帳

`config.json` → `transfer`：

| 欄位 | 預設 | 說明 |
|------|------|------|
| `enabled` | `false` | 是否啟用 |
| `target` | `""` | 對象 — 用於觸發 Discord user picker 的搜尋字串。可填顯示名稱片段或 user ID（純數字）。送指令時會打字到 `user:` 參數，再按 Enter 選最上面那位 |
| `amount` | `100` | 每次轉帳金額 |
| `interval_min` | `60` | 多久轉一次（分鐘）|

啟用後，bot 每隔 N 分鐘自動送 `/transfer`，並按下「確認轉錢」按鈕完成轉帳。設定可在 UI 按 `C` → `[J/K/L/M]` 修改。

> ⚠ **注意**：對象的搜尋字串請填夠精準的關鍵字，否則 user picker 可能選到不是你預期的人。

## 檔案說明

```
.
├── main.py                  主程式（UI + 自動化迴圈）
├── login.py                 第一次登入用
├── setup.bat                安裝環境（venv + 套件 + Chromium）
├── login.bat                執行登入
├── run.bat                  啟動 bot
├── build.bat                打包成 dist/DiscordBot.exe
├── build.spec               PyInstaller 設定檔
├── requirements.txt         Python 套件清單
├── config.example.json      設定檔範本
├── config.json              你的實際設定（已被 .gitignore，不會上傳）
├── storage_state.json       Discord session（已被 .gitignore，不會上傳）
├── slot_analysis.json       Slot 分析累計資料（已被 .gitignore，runtime 產生）
├── gambling_history.json    下注歷史紀錄（已被 .gitignore，runtime 產生）
├── bot.log                  Rotating logs（已被 .gitignore；最多 5MB×3 個檔輪替）
└── exports/                 匯出的賭博紀錄與分析報告（已被 .gitignore）
```

### 打包成 `.exe`（不需要 Python 也能跑）

雙擊執行：

```
build.bat
```

會用 PyInstaller 把整個程式打包成 `dist\DiscordBot.exe`（約 30~50 MB 的單一檔案）。

#### 部署到沒裝 Python 的電腦

1. 把 `dist\DiscordBot.exe` 複製過去
2. 放上 `config.json` 與 `storage_state.json`（與 .exe 同目錄）
3. 雙擊 `DiscordBot.exe`：
   - **第一次啟動會下載 Chromium（約 300 MB，需要網路、約 5-10 分鐘）**，存到使用者 `%LOCALAPPDATA%\ms-playwright`
   - 下載完成後直接接著啟動 bot；之後每次啟動就直接開
4. Windows Defender 可能誤判為病毒（PyInstaller 通病）— 第一次執行時請選「仍要執行」並加白名單

> 為什麼不把 Chromium 一起塞進 .exe？太肥了（會變 400+ MB），冷啟動還要解壓縮 5-10 秒。動態下載一次就好。

### 日誌與除錯

- `bot.log`：所有 INFO 以上訊息都會寫到這。檔案達 5 MB 自動輪替（保留 `bot.log.1/.2/.3` 共 3 份）
- 想看 DEBUG 等級訊息：把 `config.json` 加 `"log_level": "DEBUG"`，重啟生效
- 想清掉所有 log：按 `C` → `[X] 進階` → `[8] 清空 bot.log + 輪替檔`
- `slot_debug.log` 只在 slot 解析失敗時才寫（line/grid 沒抓到），用於 regex 排查

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

# Discord Auto Bot

以 Playwright 控制 Chromium 自動執行 Discord 斜線指令。Rich 終端 UI + Web Dashboard 雙介面。

## 功能

- **自動指令**：`/hourly` 整點對齊、`/daily`、`/slot` 賭博（auto / fixed / kelly 三策略）
- **目標 / 停損 / 連敗冷靜**：餘額達標自動 `@` 通知；跌破停損點自動停 / 階梯式下移；連敗 N 場強制冷靜 M 分鐘
- **貓娘監控**：派遣完成自動偵測 → 可選自動點「領取並再派遣」按鈕
- **自動轉帳**：定期 `/transfer` + 自動點「確認轉錢」按鈕
- **Email 通知**：達標 / 停損 / 中大獎 / bot 停擺 / 貓娘完成 / 每日 24h 摘要
- **Web Dashboard**：localhost / LAN 即時儀表板，4 個頁面 + HTTP Basic Auth + CSRF 防護
- **分析**：EV / Kelly Criterion（半 Kelly + 95% CI 下界）/ 賠率分布 / 符號 / 線路 / 連勝紀錄 / 平均時薪 / 時段分析
- **可靠性**：頻道掛了自動 reload；連續失敗自動 reboot；session 過期自動引導重新登入
- **持久化**：所有資料 SQLite + Fernet 加密；輪替日誌；可匯出 CSV / PNG

> **使用前須知**：自動化使用者帳號違反 Discord 服務條款，使用造成的帳號處置請自行承擔。僅供研究、學習用途。

## 系統需求

- Windows 10 / 11（其他 OS 未測試，啟動腳本是 .bat）
- Python 3.10+（已加入 PATH）

## 啟動（一鍵）

雙擊 `run.bat`。腳本會自動偵測並依序處理：

1. **沒 .venv** → 自動建 venv、`pip install` 套件、下載 Chromium（~300 MB，需要網路；只跑一次）
2. **套件缺漏** → 偵測到 import 失敗就重跑 `pip install`
3. **首次設定** → main.py 內 wizard 互動引導，幫你填三個 ID（伺服器 / 頻道 / 通知 user ID）
4. **首次登入** → 跳出 Chromium，手動完成 Discord 登入（含 2FA），跳到 `/channels/...` 自動儲存
5. **正常啟動** → 進 Rich UI

之後每次啟動都只跑步驟 5（除非 `requirements.txt` 變動會重跑步驟 2）。

> **怎麼取得 ID**：開啟 Discord 開發者模式（使用者設定 → 進階 → 啟用「開發者模式」），右鍵伺服器 / 頻道 / 使用者就會多出「複製 ID」選項。

## 終端機快速鍵

| 鍵 | 功能 |
|----|------|
| `Q` | 退出 |
| `C` | 修改系統設定（互動式選單，分 7 大類）|
| `P` | 暫停 / 恢復所有功能 |
| `E` | 匯出賭博紀錄（CSV + PNG + 分析報告 → `exports/`）|
| `S` | 查看 Slot 分析（EV、Kelly、賠率分布、符號統計、線路統計、時段分析）|
| `W` | 在預設瀏覽器開啟 Web Dashboard |
| `K` | 產生並開啟 QR Code 圖檔（手機掃即連 Dashboard）|
| `F` | 整個程式重啟（`run.bat` 偵測 exit code 42 自動再啟動）|

## 設定

所有設定存在加密的 SQLite (`data/bot.db`)，**不再需要編輯任何 JSON 檔**。改設定有兩種方式：

1. 終端機按 `C` 進入分類選單（賭博 / 目標停損 / 通知 / 貓娘 / 轉帳 / Dashboard / 進階）
2. Web Dashboard 的「系統設定」頁面（即時生效，不用重啟）

### 賭博基本

- `enabled`（主開關）`threshold`（保底門檻）`min_bet` `max_bet` `bet_fraction`
- `strategy`：`auto`（按比例）/ `fixed`（固定 min_bet）/ `kelly`（依 EV 動態，需 ≥ 200 筆樣本）
- `interval_min` / `interval_max`（兩次下注秒數區間）

### 目標 / 停損 / 連敗冷靜

- `goal` + `goal_action`（pause / raise）+ `goal_step` — 達標停或階梯式提目標
- `loss_floor` + `loss_action`（pause / lower_threshold）+ `loss_step` — 跌破停或下移門檻續跑
- `loss_streak_pause`（連敗 N 場觸發；0 = 停用）+ `loss_streak_cooldown_min`（冷靜 M 分鐘）

> **保底門檻 vs 停損點**：
> - `threshold`（保底）= 餘額低於此就「等待回升」（不下注、不通知）
> - `loss_floor`（停損）= 餘額低於此就「觸發動作 + 寄信」
> - 通常 `loss_floor < threshold` 才合理（先停下注，跌得更慘才真停損）

### Email 通知

`email` 區塊：`enabled` / `smtp_host:port` / `user`（寄件者）/ `password`（**Gmail 必須用 [App Password](https://myaccount.google.com/apppasswords)**）/ `to`（收件者）

七種事件可分別開關：

| 開關 | 觸發條件 |
|------|---------|
| `notify_goal` | 餘額 ≥ `goal` |
| `notify_loss` | 餘額 ≤ `loss_floor`（每段下跌只寄一次，回升後重置）|
| `notify_bigwin` | /slot「總計贏得 / 下注」≥ `bigwin_multiplier`（預設 5x）|
| `notify_dead` | /slot 或 /balance 連續失敗 ≥ `dead_threshold`（每段死亡只寄一次）|
| `notify_neko` | 貓娘派遣完成 |
| `notify_digest` | 每天 `digest_hour` 整點寄 24h 摘要（預設 0:00）|

> 設定密碼會以 Fernet 加密存進 SQLite，不會以明文留在任何 JSON。

### 貓娘監控

- `enabled` 每 `check_interval_min` 分鐘送 `/check`，剩 1 小時內加密輪詢
- `auto_claim`（預設 false）— 偵測到完成時自動 `/nekomusume status` 並點「領取並再派遣」按鈕

### 自動轉帳

- `enabled` `target`（user picker 搜尋字串：顯示名稱片段或純數字 user ID）`amount` `interval_min`
- ⚠ 對象搜尋字串請夠精準，否則 Discord 的 user picker 可能選錯人

### Web Dashboard

bot 啟動時 log 會印出本機 + LAN IP 兩個網址。

| 欄位 | 預設 | 說明 |
|------|------|------|
| `enabled` | `true` | 啟用 / 停用 |
| `host` | `"127.0.0.1"` | 預設只本機；要讓同 LAN 手機看，改 `"0.0.0.0"`（**請先設密碼**）|
| `port` | `8765` | 監聽 port |
| `username` | `"admin"` | HTTP Basic Auth 帳號 |
| `password` | `""` | HTTP Basic Auth 密碼；空 = 不啟用驗證 |

四個頁面：

- **`/` 概覽** — 餘額 / 目標 / 停損 / 連勝紀錄 / 平均時薪 / 排程倒數 / 累計淨收折線圖（含 Y 軸刻度）/ 最近 15 筆下注
- **`/analysis` 拉霸分析** — 與 `S` 鍵同等內容，HTML 表格版
- **`/logs` 即時日誌** — `bot.log` 最近 200 行，每 3 秒 tail，事件 highlight 上色
- **`/control` 系統設定** — 暫停 / 恢復、重置分析、重啟程式、即時編輯設定

> **手機怎麼連？** 改 `host` 為 `"0.0.0.0"` → 設 `password` → 按 `K` 鍵產 QR PNG → 手機掃。
>
> **資安提醒**：
> - `0.0.0.0` 沒密碼 = 同 WiFi 任何人都能控制你的 bot（暫停 / 重置 / 改設定都能做）。**務必設 `dashboard.password`**。
> - 不在 LAN 暴露的話，保持 `127.0.0.1` 即可，免設密碼。
>
> **零外部依賴**：dashboard 用 Python 內建 `http.server`，不用裝 FastAPI 等套件。

### Slot 分析與 Kelly

按 `S` 鍵看完整報告：

- **EV**（期望值）— 每注平均回報倍率（>1 = 玩家有利）
- **賠率分布**：`0` / `0~2` / `2~5` / `5~8` / `8~10` / `10~20` / `以上`（≥ 20x 列出實際倍率）
- **符號統計**：中獎次數、平均倍率、累計賠付、回收率、九宮格機率
- **線路統計**：8 種連線方向命中次數 + 命中率
- **時段分析**：依 hour-of-day 分組勝率 / 平均賠率 / 淨收（≥ 10 把的最賺/最虧時段標 🏆/💀）
- **連勝紀錄 + 平均時薪**：當前 streak、歷史最高連勝/連敗、本 session 每小時淨收

**Kelly 策略** (`strategy: "kelly"`)：

- 用 sample variance（n-1 修正）估計變異數
- 用 EV 95% 信賴區間下界估算 Kelly fraction（更保守）
- 半 Kelly + 25% 上限（避免極端建議）
- 需要 ≥ 200 筆樣本才啟用；資料不足時下 `min_bet`
- EV ≤ 1（負期望值）時固定下 `min_bet`，不停止賭博
- 隨時可從設定切回 auto / fixed

分析資料持久化在 `data/bot.db`。要重置：`C` → 進階 → 重置分析。

## 檔案結構

```
.
├── main.py             入口程式
├── run.bat             一鍵啟動腳本
├── requirements.txt
├── README.md
├── bot/                原始碼 package
│   ├── core/           DB / 加密 / 設定 schema / 狀態 / log filter
│   ├── discord/        Playwright wrapper（送指令、讀餘額、play_slot、轉帳）
│   ├── scheduler/      hourly / daily / gambling / transfer / nekomusume / digest 6 個 loop
│   ├── notifications/  email + 通知判斷
│   ├── slot/           parsers + analysis（Kelly）
│   ├── ui/             Rich 終端 UI + 互動式設定選單
│   └── web/            Web dashboard
├── data/               runtime 持久化（gitignored）
│   ├── bot.db          SQLite — 設定、歷史、分析（敏感欄位加密）
│   ├── secret.key      Fernet 加密金鑰
│   └── storage_state.json    Discord session
├── logs/               runtime 日誌（gitignored）
│   ├── bot.log + .1/.2/.3    主日誌（5MB × 3 個檔輪替）
│   └── slot_debug.log         slot 解析失敗時的 dump
└── exports/            匯出資料（gitignored）
```

## 日誌與除錯

- `logs/bot.log`：所有 INFO+ 訊息，達 5 MB 自動輪替（保留 `.1/.2/.3` 共 3 份）
- 想看 DEBUG 等級：按 `C` → 進階改 `log_level`，重啟後生效
- 想清掉所有 log：按 `C` → 進階 → 清空 log + 輪替檔
- `logs/slot_debug.log` 只在 slot 解析失敗時才寫，用於 regex 排查

## 技術說明

- **Emoji 解析**：Discord textContent 不含 `<img>` 的 alt（emoji 是 img），自家 walk DOM 把 alt 也接出來，否則 slot 符號全是空字串
- **Slot 動畫處理**：要求餘額連續 5 秒不變才採用，避免讀到「先扣下注、再加獎金」中間狀態
- **指令序列化**：所有送指令動作共用 `asyncio.Lock`，避免 6 個 loop 互相污染回應解析
- **暫停機制**：所有長 sleep 用 0.5 秒分段，可被 `P` 鍵即時打斷
- **/hourly 對齊**：等到下個整點 + 30~180 秒隨機 jitter，避免在重置邊界錯位
- **可靠性**：`recover_page()` 連續失敗 3 次 → 設 reboot 旗標 → exit 42 → run.bat 自動重 launch
- **Session 過期偵測**：頻道載入失敗時看 URL 是否在 `/login` → 自動引導重新登入
- **加密**：email password / dashboard password 用 Fernet 加密存進 SQLite，金鑰在 `data/secret.key`
- **CSRF 防護**：dashboard POST 必須 Origin/Referer 與 Host 同源
- **Log redaction**：dashboard 顯示的 log 過濾 password / token / secret 等敏感字

## 常見問題

**Q: bot 一直讀不到餘額？**  
A: 確認頻道權限正常、目標 bot 有回應、`/balance` 指令可用。看 `logs/bot.log` 是否一堆 timeout。連續失敗 ≥ 3 次會自動 reload 頻道；累積夠多會 exit 42 由 `run.bat` 重啟整個 bot。

**Q: 想換頻道？**  
A: 按 `C` → 賭博基本（或 Web Dashboard 的「系統設定」頁面）→ 改 channel_id。下次重啟生效。

**Q: 多帳號怎麼跑？**  
A: 把整個資料夾複製成 `bot1/` `bot2/`，各自有獨立 `data/bot.db` 和 `data/storage_state.json`。記得每個資料夾的 `dashboard.port` 要不一樣（例如 8765 / 8766），同時跑才不會搶 port。

**Q: 不小心搞壞 bot.db？**  
A: 刪掉 `data/bot.db`，下次啟動 wizard 會重建（但歷史資料會清空）。`data/secret.key` 也別刪，否則密碼欄位無法解密。

**Q: 從 v1（root JSON 版本）升級？**  
A: 啟動時自動偵測 `config.json` / `slot_analysis.json` / `gambling_history.json` 並一次性遷移到 `data/bot.db`。遷移完那些 JSON 就可以刪。

**Q: 我帳號被 Discord 鎖了 / Captcha？**  
A: Bot 帳號自動化違反 Discord ToS。請自行承擔風險。如果頻繁觸發，把 `interval_min/max` 加大、`/hourly` jitter 加大、減少 transfer / 貓娘的頻率。

# Discord Auto Bot

以 Playwright 控制 Chromium 自動執行 Discord 斜線指令。Rich 終端 UI + Web Dashboard 雙介面。

## 功能

- **自動指令**:`/hourly` 整點對齊、`/daily`(每日 00:00 觸發)、`/slot` 賭博(auto / fixed / kelly 三策略)
- **目標 / 停損 / 連敗冷靜**:餘額達標自動 `@` 通知;跌破停損點自動停 / 階梯式下移;連敗 N 場強制冷靜 M 分鐘
- **進階下注策略**(全部 opt-in):時段過濾 / 滾動視窗 EV 動態下注 / Trailing Stop
- **貓娘監控**:派遣完成自動偵測 → 可選自動點「領取並再派遣」按鈕
- **自動轉帳**:定期 `/transfer` + 自動點「確認轉錢」按鈕
- **股票監視 / 建議**:`/stock` + `/portfolio` 自動抓全部股票 + 持股 + 做空倉位 + 技術分析(MA 黃金/死亡交叉、獲利了結、停損)+ 短期波動警示
- **股票新聞抓取**:獨立 loop 每 60 分鐘對所有股票抓近期新聞 → UI + email
- **Email 通知**:8 種事件可分別開關(達標 / 停損 / 中大獎 / bot 停擺 / 貓娘完成 / 每日 24h 摘要 / 股票強訊號 / 股票新聞)
- **🐛 除錯訊息頻道**:把 WARNING+ 紀錄推到獨立 Discord 頻道,手機 Discord 也能即時看到 bot 出狀況
- **獨立頻道架構**:股票指令 / 新聞 / 貓娘 / 除錯各自可設獨立頻道,避免不同指令 ephemeral 互相污染 parser
- **自動版本更新**:每 N 分鐘檢查 GitHub 新 commit,可開啟 auto_update 自動 git pull + 重啟
- **Web Dashboard**:localhost / LAN 即時儀表板,5 個頁面 + HTTP Basic Auth + CSRF 防護
- **分析**:EV / Kelly Criterion(半 Kelly + 95% CI 下界)/ 賠率分布 / 符號 / 線路 / 連勝紀錄 / 平均時薪 / 時段分析 / 股票買賣訊號評分
- **Loop 健康追蹤**:每個 loop 有 idle/running/ok/failed/auto_paused 狀態,連續失敗自動暫停 30 分鐘冷卻,UI footer 顯示
- **可靠性**:頻道掛了自動 reload;連續失敗自動 reboot;session 過期自動引導重新登入
- **持久化**:所有資料 SQLite + Fernet 加密;輪替日誌;可匯出 CSV / PNG

> **使用前須知**:自動化使用者帳號違反 Discord 服務條款,使用造成的帳號處置請自行承擔。僅供研究、學習用途。

## 系統需求

- Windows 10 / 11(其他 OS 未測試,啟動腳本是 .bat)
- Python 3.10+(已加入 PATH)

## 啟動(一鍵)

雙擊 `run.bat`。腳本會自動偵測並依序處理:

1. **沒 .venv** → 自動建 venv、`pip install` 套件、下載 Chromium(~300 MB,需要網路;只跑一次)
2. **套件缺漏** → 偵測到 import 失敗就重跑 `pip install`
3. **首次設定** → main.py 內 wizard 互動引導,幫你填基本 ID(伺服器 / 主頻道 / 通知 user ID);啟用股票 / 貓娘 / 除錯頻道時也會要求對應頻道 ID
4. **首次登入** → 跳出 Chromium,手動完成 Discord 登入(含 2FA),跳到 `/channels/...` 自動儲存
5. **正常啟動** → 進 Rich UI

之後每次啟動都只跑步驟 5(除非 `requirements.txt` 變動會重跑步驟 2)。

> **怎麼取得 ID**:開啟 Discord 開發者模式(使用者設定 → 進階 → 啟用「開發者模式」),右鍵伺服器 / 頻道 / 使用者就會多出「複製 ID」選項。

## 終端機快速鍵

| 鍵 | 功能 |
|----|------|
| `Q` | 退出 |
| `C` | 修改系統設定(互動式選單,分 9+ 大類)|
| `P` | 暫停 / 恢復所有功能 |
| `E` | 匯出賭博紀錄(CSV + PNG + 分析報告 → `exports/`)|
| `S` | 查看 Slot 分析(EV、Kelly、賠率分布、符號統計、線路統計、時段分析)|
| `T` | 查看股票分析(摘要 + Top 推薦 + 持股 + 做空 + 全部股票 + 近期新聞)|
| `W` | 在預設瀏覽器開啟 Web Dashboard |
| `K` | 產生並開啟 QR Code 圖檔(手機掃即連 Dashboard)|
| `X` | 全螢幕除錯紀錄(最近 30 筆 WARNING+,按 level 上色)|
| `F` | 整個程式重啟(`run.bat` 偵測 exit code 42 自動再啟動)|

主面板 footer 的「X 除錯」字樣會按 level 自動上色:有 ERROR/CRITICAL → 紅、只有 WARNING → 黃、無 → 預設色。

## 設定

所有設定存在加密的 SQLite (`data/bot.db`),**不再需要編輯任何 JSON 檔**。改設定有三種方式:

1. 終端機按 `C` 進入分類選單(賭博 / 目標停損 / 通知 / 貓娘 / 轉帳 / Dashboard / 進階策略 / 股票 / 版本更新 / 除錯頻道 / 進階檔案管理)
2. Web Dashboard 的「系統設定」頁面(即時生效,不用重啟)
3. 從 Discord 直接看 bot 狀況(若有設除錯頻道,WARNING+ 訊息會推到 Discord)

### 賭博基本

- `enabled`(主開關)`threshold`(保底門檻)`min_bet` `max_bet` `bet_fraction`
- `strategy`:`auto`(按比例)/ `fixed`(固定 min_bet)/ `kelly`(依 EV 動態,需 ≥ 200 筆樣本)
- `interval_min` / `interval_max`(兩次下注秒數區間)

### 目標 / 停損 / 連敗冷靜

- `goal` + `goal_action`(pause / raise)+ `goal_step` — 達標停或階梯式提目標
- `loss_floor` + `loss_action`(pause / lower_threshold)+ `loss_step` — 跌破停或下移門檻續跑
- `loss_streak_pause`(連敗 N 場觸發;0 = 停用)+ `loss_streak_cooldown_min`(冷靜 M 分鐘)

> **保底門檻 vs 停損點**:
> - `threshold`(保底)= 餘額低於此就「等待回升」(不下注、不通知)
> - `loss_floor`(停損)= 餘額低於此就「觸發動作 + 寄信」
> - 通常 `loss_floor < threshold` 才合理(先停下注,跌得更慘才真停損)

> **目標達成 raise 模式**:達標後 `threshold` 自動改為 `goal - 10000`,保留 10000 緩衝給 bot 繼續下注,不會立刻撞保底。

### 進階下注策略(opt-in)

預設全部停用。需要時去設定選單 `[C] → [7] 🎯 進階下注策略` 啟用:

| 策略 | 功能 | 注意 |
|------|------|------|
| **hourly_filter** | 跳過歷史 EV / 勝率差的小時(per hour-of-day 分析)| 該小時樣本 < `hourly_min_bets` 時不過濾 |
| **rolling EV** | 近期 EV 差時減碼、好時加碼(滑動視窗)| 不改變期望值,只降 variance / drawdown |
| **trailing_stop** | 累計淨收從峰值跌幅 > X% → 暫停 N 分鐘 | 冷卻結束 baseline 重設,避免立刻又觸發 |

> 這些策略**不會把負 EV 變成正**,只能降低 variance / drawdown。Dashboard 有「策略 backtest」可看歷史模擬結果。

### Email 通知

`email` 區塊:`enabled` / `smtp_host:port` / `user`(寄件者)/ `password`(**Gmail 必須用 [App Password](https://myaccount.google.com/apppasswords)**)/ `to`(收件者)

8 種事件可分別開關:

| 開關 | 觸發條件 |
|------|---------|
| `notify_goal` | 餘額 ≥ `goal` |
| `notify_loss` | 餘額 ≤ `loss_floor`(每段下跌只寄一次,回升後重置)|
| `notify_bigwin` | /slot「總計贏得 / 下注」≥ `bigwin_multiplier`(預設 5x)|
| `notify_dead` | /slot 或 /balance 連續失敗 ≥ `dead_threshold`(每段死亡只寄一次)|
| `notify_neko` | 貓娘派遣完成 |
| `notify_digest` | 每天 `digest_hour` 整點寄 24h 摘要(預設 0:00)|
| `notify_stock_signal` | 某支股 buy/sell score ≥ `signal_score_threshold`(預設 80)|
| `notify_stock_news` | news loop 抓到新新聞(DB UNIQUE 去重後真的是新項才寄)|

> 設定密碼會以 Fernet 加密存進 SQLite,不會以明文留在任何 JSON。

### 貓娘監控

- `enabled` 每 `check_interval_min` 分鐘送 `/check`,剩 1 小時內加密輪詢
- `auto_claim`(預設 false)— 偵測到完成時自動 `/nekomusume status` 並點「領取並再派遣」按鈕
- `channel_id` — 獨立貓娘頻道(wizard 強制設定),`/check` 跟 `/nekomusume` 切到這頻道送,避免跟主頻道的 slot / hourly 混

### 自動轉帳

- `enabled` `target`(user picker 搜尋字串:顯示名稱片段或純數字 user ID)`amount` `interval_min`
- 連續失敗會自動進入 `auto_paused` 冷卻 30 分鐘(避免 spam 失敗指令)
- ⚠ 對象搜尋字串請夠精準,否則 Discord 的 user picker 可能選錯人

### 股票監視 / 建議

設定選單 `[C] → [8] 📈 股票監視`:

- `enabled` `poll_interval_min`(預設 15 分鐘)— 抓 `/stock`(全部股票 + 趨勢)+ `/portfolio`(持股 + 平均成本 + 盈虧)+ 做空倉位
- `ma_short` / `ma_long` — 短/長均線參數(預設 5/20,看近期 vs 大方向)
- `take_profit_pct` / `stop_loss_pct` — 持股獲利/虧損達 % 建議賣
- `signal_score_threshold`(預設 80)— 評分高於此才視為「強訊號」,寫進日誌 + 寄 email
- **短期波動警示**(opt-in):比較最近 N 分鐘價格變動,超過 X% 提醒(同 sym 同方向有冷卻避免洗版)
- **新聞抓取**(獨立 loop):`news_poll_interval_min`(預設 60 分鐘)對所有抓到的股票送 `/stock symbol:X` + 點「近期新聞」按鈕,新項寫進 DB(UNIQUE 去重)+ UI + email

**獨立頻道**(wizard 在 `enabled=True` 時強制設定):
- `stock_channel_id` — stock_loop 跑 `/stock` `/portfolio` 切過去
- `news_channel_id` — news_loop 跑新聞抓取切過去

> **不會自動下單**。Phase 1-2 純建議,user 看訊號自己手動買賣。Discord modal 太脆弱所以 trade 自動化已移除(commit `d18b4df`),不會回來。

> **買賣訊號互斥**:黃金交叉 → +15 多頭(buy 加分);死亡交叉 → -15 空頭(sell 加分,不是 buy 加分)。

### 🐛 除錯訊息頻道

設定選單 `[C] → [B] 🐛 除錯頻道`:

把 bot 運行中的 WARNING+ 紀錄推到一個獨立 Discord 頻道,跟 X 鍵除錯紀錄、`bot.log` 並行。用途:user 不在電腦旁時手機 Discord 也能看到 bot 出狀況(transfer 失敗 / parser 跳掉 / session 過期 等)。

| 設定 | 預設 | 說明 |
|------|------|------|
| `enabled` | `false` | 啟用 / 停用 |
| `channel_id` | `""` | Discord 頻道 ID(wizard 強制設;不可跟主/股票/新聞/貓娘頻道重疊)|
| `min_level` | `WARNING` | 最低 level(WARNING / ERROR / CRITICAL)|
| `poll_interval_sec` | `60` | 多久 flush 一次 pending queue 到 Discord |
| `max_per_flush` | `5` | 一次最多打包幾筆(防 spam,上限 10)|
| `include_logger_name` | `true` | 訊息是否含 logger 名 |

訊息格式:

```
**Debug** · 3 筆 · 2026-05-11
⚠️ `20:32:44` **scheduler.gambling** — 連敗 5 場 ≥5,暫停下注 5.0 分鐘
❌ `20:33:15` **discord.client** — recover_page 連續 3 次失敗
🚨 `20:34:01` **scheduler.news** — HOLO 沒抓到 news_text
```

各 level 配色 emoji:WARNING ⚠️ / ERROR ❌ / CRITICAL 🚨。

子選單裡有 `[T] 立即測試送一筆到頻道` 跟 `[C] 清空待送 queue` 方便調試。

### 版本更新

設定選單 `[C] → [9] 🔄 版本更新`:

- `auto_check`(預設 true)每 `check_interval_min` 分鐘做一次 `git ls-remote` 跟本地 commit hash 比對
- 偵測到新版 → UI 顯示 🔔 + queue_log + 可選 email
- `auto_update`(預設 false)— 偵測新版自動 git pull + 重啟(慎用,本地未提交修改會中斷)
- 選單裡 `[4] 立即檢查 / 更新` 手動觸發

### Web Dashboard

bot 啟動時 log 會印出本機 + LAN IP 兩個網址。

| 欄位 | 預設 | 說明 |
|------|------|------|
| `enabled` | `true` | 啟用 / 停用 |
| `host` | `"127.0.0.1"` | 預設只本機;要讓同 LAN 手機看,改 `"0.0.0.0"`(**請先設密碼**)|
| `port` | `8765` | 監聽 port |
| `username` | `"admin"` | HTTP Basic Auth 帳號 |
| `password` | `""` | HTTP Basic Auth 密碼;空 = 不啟用驗證 |

5 個頁面:

- **`/` 概覽** — 餘額 / 目標 / 停損 / 連勝紀錄 / 平均時薪 / 排程倒數 / 累計淨收折線圖(含 Y 軸刻度)/ 最近 15 筆下注
- **`/analysis` 拉霸分析** — 與 `S` 鍵同等內容,HTML 表格版
- **`/stocks` 股票** — 5 個 tab:總覽 + Top 3 推薦 + 近期新聞 / 持股(含趨勢欄 + 做空明細 table)/ 買進建議 / 賣出建議 / 全部股票(含趨勢欄)
- **`/logs` 即時日誌** — 🐛 除錯紀錄 card(WARNING+ 最近 30 筆,按 level 上色)+ 📋 Bot Log 文字流(每 3 秒 tail)
- **`/control` 系統設定** — 暫停 / 恢復、重置分析、重啟程式、即時編輯設定

> **手機怎麼連?** 改 `host` 為 `"0.0.0.0"` → 設 `password` → 按 `K` 鍵產 QR PNG → 手機掃。
>
> **資安提醒**:
> - `0.0.0.0` 沒密碼 = 同 WiFi 任何人都能控制你的 bot(暫停 / 重置 / 改設定都能做)。**務必設 `dashboard.password`**。
> - 不在 LAN 暴露的話,保持 `127.0.0.1` 即可,免設密碼。
>
> **零外部依賴**:dashboard 用 Python 內建 `http.server`,不用裝 FastAPI 等套件。

### Slot 分析與 Kelly

按 `S` 鍵看完整報告:

- **EV**(期望值)— 每注平均回報倍率(>1 = 玩家有利)
- **賠率分布**:`0` / `0~2` / `2~5` / `5~8` / `8~10` / `10~20` / `以上`(≥ 20x 列出實際倍率)
- **符號統計**:中獎次數、平均倍率、累計賠付、回收率、九宮格機率
- **線路統計**:8 種連線方向命中次數 + 命中率
- **時段分析**:依 hour-of-day 分組勝率 / 平均賠率 / 淨收(≥ 10 把的最賺/最虧時段標 🏆/💀)
- **連勝紀錄 + 平均時薪**:當前 streak、歷史最高連勝/連敗、本 session 每小時淨收

**Kelly 策略** (`strategy: "kelly"`):

- 用 sample variance(n-1 修正)估計變異數
- 用 EV 95% 信賴區間下界估算 Kelly fraction(更保守)
- 半 Kelly + 25% 上限(避免極端建議)
- 需要 ≥ 200 筆樣本才啟用;資料不足時下 `min_bet`
- EV ≤ 1(負期望值)時固定下 `min_bet`,不停止賭博
- 隨時可從設定切回 auto / fixed

分析資料持久化在 `data/bot.db`。要重置:`C` → 進階 → 重置分析。

## 檔案結構

```
.
├── main.py             入口程式(啟動 10 個 loop + UI + dashboard)
├── run.bat             一鍵啟動腳本(偵測 exit 42 / data/.reboot 自動重啟)
├── requirements.txt
├── README.md
├── bot/                原始碼 package
│   ├── core/           DB / 加密 / 設定 schema / 狀態 / log filter / updater
│   ├── discord/        Playwright wrapper(送指令、讀餘額、play_slot、portfolio、shorts、新聞)
│   ├── scheduler/      10 個 loop:
│   │                     hourly / daily / gambling / transfer / nekomusume
│   │                     digest / stock / news / updater / debug
│   ├── notifications/  email + digest + stock notification 判斷
│   ├── slot/           parsers + analysis(Kelly + EV + 三進階策略)
│   ├── stock/          parser(portfolio / shorts / detail / news)+ analysis(buy/sell signal + volatility)
│   ├── ui/             Rich 終端 UI + 互動式設定選單 + stock_view + error_view + wizard + maintenance
│   └── web/            Web dashboard(5 頁,純 stdlib http.server)
├── data/               runtime 持久化(gitignored)
│   ├── bot.db          SQLite — 設定 / 歷史 / 分析 / 新聞 / 股價(敏感欄位加密)
│   ├── secret.key      Fernet 加密金鑰
│   ├── storage_state.json    Discord session
│   └── .reboot          重啟 sentinel(run.bat 讀取後刪)
├── logs/               runtime 日誌(gitignored)
│   ├── bot.log + .1/.2/.3    主日誌(5 MB × 3 個檔輪替)
│   ├── slot_debug.log         slot 解析失敗時的 dump
│   └── stock_debug.log        stock 解析失敗時的 dump
└── exports/            匯出資料(gitignored)
```

## 日誌與除錯

- `logs/bot.log`:所有 INFO+ 訊息,達 5 MB 自動輪替(保留 `.1/.2/.3` 共 3 份)
- `logs/slot_debug.log` / `logs/stock_debug.log`:只在 parser 失敗時才寫,用於 regex 排查
- 想看 DEBUG 等級:按 `C` → 進階改 `log_level`,重啟後生效
- 想清掉所有 log:按 `C` → 進階 → 清空 log + 輪替檔
- **三層除錯管道**:
  1. 終端按 `X` — 最近 30 筆 WARNING+,全螢幕、按 level 上色
  2. Web Dashboard `/logs` 頁 — 🐛 除錯紀錄 card(WARNING+)+ bot.log 文字流
  3. Discord 除錯頻道(若啟用)— 推 WARNING+ 到獨立頻道,手機 Discord 看

- **UI 日誌分兩欄**:主面板下方左欄「📋 系統 + 拉霸」(hourly / daily / gambling / transfer / neko / debug)+ 右欄「📈 股票 / 新聞」(stock / news / 以及 stock/news task 內的 bot.discord.client log)

## 開發者

`pyproject.toml` 內含 ruff 設定。要跑:

```bash
pip install ruff
python -m ruff check .         # 列出所有 lint issue
python -m ruff check . --fix   # 自動修能修的
python -m ruff format .        # 格式化
```

ruff 不是 runtime dependency,只在你想 lint / format 時裝。

## 技術說明

- **Emoji 解析**:Discord textContent 不含 `<img>` 的 alt(emoji 是 img),自家 walk DOM 把 alt 也接出來,否則 slot 符號全是空字串
- **Slot 動畫處理**:要求餘額連續 5 秒不變才採用,避免讀到「先扣下注、再加獎金」中間狀態
- **指令序列化**:所有送指令動作共用 `command_lock`(asyncio.Lock),避免 10 個 loop 互相污染回應解析
- **`channel_context` 機制**:stock / news / neko / debug 各自獨立頻道,進 loop 時整段持 command_lock + navigate 到 target channel + 跑完切回主頻道
- **Ephemeral 累積處理**:Discord ephemeral 不替換而是累積在 page textContent(舊的不消失)。Parser 用 `rfind` + anchor slice 只解析「最新一則 ephemeral」,避免抓到已賣出 / 已平倉的舊資料
- **暫停機制**:所有長 sleep 用 0.5 秒分段 `interruptible_sleep`,可被 `P` 鍵即時打斷;每個 loop 開頭 `wait_while_paused` 原地等
- **/hourly 對齊**:等到下個整點 + 30~180 秒隨機 jitter,避免在重置邊界錯位
- **/daily 對齊**:每日 00:00 整點觸發,DB meta `last_daily_fired_ts` 跨 reboot 防重複,啟動補跑
- **Loop 健康追蹤**:每個 loop `mark_loop_running` / `mark_loop_ok` / `mark_loop_failed`,連續失敗 5 次自動 `auto_paused` 冷卻 30 分鐘
- **UI log 分流**:按 logger name(`bot.scheduler.stock/news/...`)+ 當前 asyncio task name(`stock`/`news`)雙重判斷,讓 stock/news task 內的 `bot.discord.client` log 也推右欄
- **可靠性**:`recover_page()` 連續失敗 3 次 → 設 reboot 旗標 → exit 42 → run.bat 自動重 launch
- **Session 過期偵測**:頻道載入失敗時看 URL 是否在 `/login` → 自動引導重新登入。連續失敗 ≥ 3 次跨 reboot 才停下不再 wizard(避免無限循環)
- **加密**:email password / dashboard password 用 Fernet 加密存進 SQLite,金鑰在 `data/secret.key`
- **CSRF 防護**:dashboard POST 必須 Origin/Referer 與 Host 同源
- **Log redaction**:dashboard 顯示的 log 過濾 password / token / secret 等敏感字
- **Debug 頻道 feedback loop 防護**:UILogHandler push WARNING+ 到 `debug_pending` 時用 `asyncio.current_task().get_name() == "debug"` 過濾,避免 debug_loop 自身失敗 log 觸發無限遞迴
- **`_send_message` emoji 訊息處理**:Discord 把 emoji 渲染成 `<img>`,textContent 拿不到 alt,所以「input 內容比對」用 ASCII signature(剝掉 emoji / 中文 / 空白)避免重複插入

## 常見問題

**Q: bot 一直讀不到餘額?**
A: 確認頻道權限正常、目標 bot 有回應、`/balance` 指令可用。看 `logs/bot.log` 是否一堆 timeout。連續失敗 ≥ 3 次會自動 reload 頻道;累積夠多會 exit 42 由 `run.bat` 重啟整個 bot。

**Q: 想換頻道?**
A: 按 `C` → 賭博基本(或 Web Dashboard 的「系統設定」頁面)→ 改 channel_id。下次重啟生效。

**Q: 股票 / 貓娘 / 新聞用同一個頻道行嗎?**
A: 技術上可行(設一樣的 channel_id),但**強烈不建議**。不同指令的 ephemeral 會互相累積在 page 上,parser 可能誤抓舊資料。建議:主頻道跑 slot/hourly/daily/transfer,股票指令一個頻道、新聞一個、貓娘一個、除錯一個(共 5 個頻道)。

**Q: 補回做空後 bot 還顯示已平倉的 symbol?**
A: 已修(commit `ffb4348`)。Discord ephemeral 累積,parser 之前會吃到舊 ephemeral 的資料。現在切只抓「本次點 button 後新增的 ephemeral」。同理 holdings 賣股後也會即時更新。

**Q: 多帳號怎麼跑?**
A: 把整個資料夾複製成 `bot1/` `bot2/`,各自有獨立 `data/bot.db` 和 `data/storage_state.json`。記得每個資料夾的 `dashboard.port` 要不一樣(例如 8765 / 8766),同時跑才不會搶 port。

**Q: 不小心搞壞 bot.db?**
A: 刪掉 `data/bot.db`,下次啟動 wizard 會重建(但歷史資料會清空)。`data/secret.key` 也別刪,否則密碼欄位無法解密。

**Q: 從舊版(root JSON)升級?**
A: 啟動時自動偵測 `config.json` / `slot_analysis.json` / `gambling_history.json` 並一次性遷移到 `data/bot.db`。遷移完那些 JSON 就可以刪。

**Q: 除錯頻道訊息一直沒出現?**
A:
1. 設定選單 `[B] → [T] 立即測試送一筆` 看會不會送
2. 確認 bot 有權限發訊息到該頻道
3. `min_level` 設太嚴(例 CRITICAL)會把 WARNING 篩掉
4. 按 X 看「除錯 loop 自身有沒有失敗」(連續 5 次失敗會 auto_paused 冷卻 30 分鐘)

**Q: stock auto_paused 怎麼手動恢復?**
A: 按 `T` 進股票檢視 → 按 `R + Enter` 立即重 poll(會 override auto_paused 冷卻)。或 Dashboard 概覽頁有「立即重 poll 股票」按鈕。

**Q: 我帳號被 Discord 鎖了 / Captcha?**
A: Bot 帳號自動化違反 Discord ToS。請自行承擔風險。如果頻繁觸發,把 `interval_min/max` 加大、`/hourly` jitter 加大、減少 transfer / 貓娘的頻率。

# 右側加碼雷達（twradar）

![Python](https://img.shields.io/badge/python-3.10%2B-blue) ![License: MIT](https://img.shields.io/badge/license-MIT-green) ![Dependencies](https://img.shields.io/badge/dependencies-none-lightgrey)

台股盤後籌碼自動爬取 → SQLite 儲存 → 「右側加碼」訊號分析 → Web API 與網頁 UI（含可列印 PDF 報表）。
只用 Python 標準函式庫，不需要資料庫伺服器；macOS 與 Linux 都能一鍵安裝。

*A self-hosted Taiwan stock "right-side entry" signal tracker: nightly crawler for TWSE/TPEx institutional flows, margin balances and prices, stored in SQLite, analyzed with a rule engine, and served through a token-protected JSON API and web dashboard. Pure Python standard library.*

> ⚠️ 本專案僅整理公開籌碼資料並依規則產生訊號，**不構成任何投資建議**。使用者須自行承擔投資風險。

## 功能

- **盤後自動爬取**：證交所、櫃買中心官方全市場資料（收盤行情、三大法人、融資融券、法人金額），每次請求間隔 8 秒以上；官方來源失敗時改用 FinMind 逐檔備援。
- **SQLite 儲存**：全市場每日資料都存下來，新增觀察個股時月線、季線立刻算得出來。
- **右側加碼判定**：法人、主力、散戶、價量四項檢查，產生 🟢 加碼訊號／🟡 觀察中／🔴 不宜加碼，並列出「還差什麼」。門檻可調。
- **有狀態的 Web API**：每個回應都標明抓取與分析是否完成（`status`、`ready`），方便排程或其他程式接手。
- **網頁 UI**：總覽、個股圖表（收盤／月線／季線、法人、融資）、A4 報表（列印成 PDF）、觀察清單與門檻設定、系統狀態。
- **匯出**：JSON、CSV（Excel 可直接開啟）、整個 SQLite 資料庫。
- **一鍵部署**：Mac 點兩下安裝、Linux 一行指令；自動設定開機執行（launchd／systemd）；也提供 Docker。

| 總覽 | 個股 | 報表 |
|---|---|---|
| ![總覽](docs/images/dashboard.png) | ![個股](docs/images/stock.png) | ![報表](docs/images/report.png) |

<sub>截圖使用測試用的模擬資料。</sub>

## 快速開始（開發者）

```bash
git clone https://github.com/auxotsgh86/twradar.git
cd twradar
python3 -m twradar serve          # 不需要安裝任何套件；預設 http://localhost:8765
python3 -m twradar info           # 顯示網址、token 與初始管理密碼
python3 -m twradar doctor         # 測試資料來源是否正常
```

資料預設存在 `~/twradar-data`，可用環境變數 `TWRADAR_HOME` 指定。

---

## 一、給借機器的人：安裝只要 3 步

**Mac**
1. 把收到的 zip 檔（或 GitHub 頁面「Code → Download ZIP」下載的檔案）點兩下解壓縮。
2. 打開解壓縮後的資料夾，在「安裝（Mac 請點兩下）.command」上 **按右鍵 → 打開 → 再按一次「打開」**
   （第一次一定要用右鍵打開，直接點兩下會被 Mac 擋下）。
3. 等視窗出現「✅ 安裝完成！」。瀏覽器會自動打開網頁；桌面會多一個「右側加碼雷達-連線資訊.txt」，把這個檔案傳給 Paul 就完成了（檔案裡有管理密碼，請只傳給 Paul）。

**Linux**
```bash
git clone https://github.com/auxotsgh86/twradar.git && cd twradar && bash deploy/install.sh
```

**之後請保持：** 電腦開機、接上電源、不要進入睡眠（Mac：系統設定 → 電池／能源 → 開啟「防止自動進入睡眠」）。
程式會在開機後自動執行，不需要任何操作。

---

## 二、給維護者（Paul）

### 安裝後的位置
| 項目 | 位置 |
|---|---|
| 程式 | `~/twradar-app` |
| 資料庫 | `~/twradar-data/twradar.sqlite3` |
| 使用者設定 | `~/twradar-data/settings.json`（觀察清單、門檻、資料源、排程、顏色；網頁可改，舊版本在 `settings.history/`） |
| 系統設定 | `~/twradar-data/config.json`（port、token、管理密碼雜湊；網頁不能改） |
| 紀錄 | `~/twradar-data/twradar.log` |
| 服務 | Mac：`~/Library/LaunchAgents/tw.radar.plist`；Linux：`systemctl --user status twradar` |

更新版本：解壓新版後重跑安裝即可，資料與 token 保留。移除：`bash deploy/uninstall.sh`（加 `--purge` 連資料一起刪）。

### 權限與密碼

| 身分 | 用什麼登入 | 可以做 |
|---|---|---|
| 唯讀 | 唯讀 token（網址 `?token=` 或標頭） | 查看所有資料（晨報用這個） |
| 管理者 | 網頁輸入管理密碼，或管理 token（只能放標頭） | 改觀察清單、門檻、設定，手動抓取，匯出資料庫，查看與重設 token |

- 初始管理密碼在安裝完成時顯示（也在桌面的連線資訊檔），登入後到「設定 → 權限」改成自己的密碼。
- 忘記 token：用管理密碼登入，在「設定 → 權限」查看或重設。
- 忘記管理密碼：在主機上執行 `cd ~/twradar-app && TWRADAR_HOME=~/twradar-data .venv/bin/python -m twradar admin reset-password`。
- 從 v0.1 升級：原本的 token 自動變成唯讀 token（晨報網址不用改）；觀察清單與門檻自動搬到 `settings.json`。

### 第一次啟動會發生什麼
1. 建立預設觀察清單（13 檔，可在網頁「設定與狀態」修改，上限 50 檔）。
2. 背景補抓約 100 個交易日的全市場資料（每次請求間隔約 8～10 秒，約 1～2 小時），進度顯示在「設定與狀態」。
3. 之後每個交易日自動排程（台北時間，可在 `settings.json` 的 `schedule` 修改）：

| 時間 | 動作 |
|---|---|
| 17:40 | 抓當天收盤行情、三大法人 |
| 21:50 | 補抓融資融券等較晚公布的資料 |
| 23:40 | 再補一次 |
| 隔天 06:40 | 前一交易日最後一次補抓；仍缺的資料標為「部分缺漏」 |

每次抓完會自動重新分析。

### 確認資料來源是否正常
```bash
cd ~/twradar-app && TWRADAR_HOME=~/twradar-data .venv/bin/python -m twradar doctor
```
會逐一測試證交所、櫃買中心、FinMind 是否可連線、格式是否能解析。**部署後請先跑一次。**
可加 `--date 2026-11-11` 指定測試日。若櫃買中心有項目失敗，再執行 `probe`（同樣可加 `--date`），
它會列出每個候選網址實際回傳的日期、欄位與第一列資料，把輸出交給維護者即可修正。

### 讓 Claude 晨報從外部讀取 API
機器通常在家用網路後面，需要一個固定的 HTTPS 網址。擇一：

- **Tailscale Funnel（推薦，免費、網址固定）**：在該機器安裝 Tailscale 並登入，執行
  `tailscale funnel --bg 8765`，會得到 `https://<機器名>.<tailnet>.ts.net`。
- **Cloudflare Tunnel**：有自己的網域時使用 named tunnel，指向 `http://localhost:8765`。

拿到網址後，把「網址 + token」交給 Claude，晨報排程就會改成從 API 讀資料。
API 全部需要 token，請不要公開分享含 token 的網址。

---

## 三、API

認證：唯讀 token 可用 `Authorization: Bearer <token>`、`X-Token: <token>`，或網址加 `?token=<token>`（方便只能發 GET 的工具）。
標示「管理」的端點需要管理 token（只接受標頭）或管理者登入；所有非 GET 的請求都需要管理者。

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/api/v1/health` | 服務是否在線（不需 token） |
| GET | `/api/v1/latest` | 最新交易日的完整結果 |
| GET | `/api/v1/latest?ready=1` | 最新「抓取＋分析都完成」的交易日 |
| GET | `/api/v1/days/{YYYY-MM-DD}` | 指定日；`?format=csv`、`radar`（可匯入 claude.ai 雷達工具）、`status`（只回狀態） |
| GET | `/api/v1/stocks/{code}/history?days=120` | 個股歷史（含 MA20/MA60、法人、融資）；`&format=csv` |
| GET | `/api/v1/dates` | 所有日期與狀態 |
| GET | `/api/v1/status` | 系統狀態、排程、補抓進度 |
| GET/POST/DELETE | `/api/v1/watchlist` | 觀察清單（POST 為管理；JSON：`code,name,market,group,note,keyLevel,holding,cost,shares`） |
| GET/PUT | `/api/v1/rules` | 判定門檻（PUT 為管理） |
| POST | `/api/v1/crawl` `/api/v1/analyze` `/api/v1/backfill` | 手動觸發（管理） |
| GET | `/api/v1/export/sqlite` | 下載整個資料庫（管理） |
| GET/PUT | `/api/v1/settings` | 全部設定（管理；token 類欄位遮蔽顯示，送回遮蔽值代表不變；不合法回 400 並列出每個錯誤的位置） |
| GET / POST | `/api/v1/settings/history`、`/settings/restore`（`{id}`）、`/settings/reset`（`{section}`）、`/settings/export` | 設定版本與還原（管理） |
| POST | `/api/v1/auth/login`（`{password}`）、`/auth/logout`；GET `/auth/me` | 網頁登入（不需 token） |
| GET / POST | `/api/v1/auth/tokens`、`/auth/rotate`（`{kind: read\|admin}`）、`/auth/password`（`{current,new}`） | 查看／重設 token、改密碼（管理） |

### 回應一定帶狀態
```json
{
  "date": "2026-11-11",
  "status": "complete",
  "ready": true,
  "statusText": "抓取與分析皆已完成",
  "crawl":    { "status": "complete", "attempts": 2, "finishedAt": "...", "steps": { "twse_margin": {"status": "ok"} } },
  "analysis": { "status": "complete", "analyzedAt": "..." },
  "market": { "taiex": 44257.15, "taiexChgPct": 3.62, "foreignAmt": -7.66, "unit": "億元" },
  "summary": { "go": 0, "wait": 5, "stop": 7, "na": 1 },
  "stocks": [ { "code": "2330", "data": { "close": 2395, "ma20": 2362.75 }, "signal": { "light": "wait", "needs": ["..."] } } ]
}
```

| `status` | 意思 |
|---|---|
| `not_started` | 這天還沒開始抓 |
| `running` | 抓取中 |
| `waiting` | 部分資料交易所尚未公布，會在下個排程自動重抓 |
| `complete` | 全部資料已抓到 |
| `partial` | 最後一次嘗試後仍有缺漏（缺什麼寫在 `crawl.message`、`crawl.steps`） |
| `failed` | 全部失敗（網路或被封鎖） |
| `holiday` | 休市 |

`analysis.status`：`not_started` → `provisional`（依不完整資料先算的暫定結果）→ `complete`。
**`ready: true` 只在 `status=complete` 且 `analysis.status=complete` 時成立**，自動化程式請以它為準。

### 欄位（張為單位，除非另註）
`close` 收盤、`chgPct` 漲跌幅%、`vol` 成交量、`vol5` 前 5 日均量、`ma20`/`ma60` 月線/季線、`prevHigh` 前 20 日最高收盤、
`foreign`/`trust`/`dealer` 外資/投信/自營商買賣超、`foreignStreak`/`trustStreak` 連續買(+)/賣(−)天數、
`foreign5d`/`trust5d` 近 5 日累計、`marginChg`/`shortChg` 融資/融券增減。

---

## 四、判定規則
完整說明見 [docs/STRATEGY.md](docs/STRATEGY.md)（v0.3，已經專家審閱）。摘要：
- 進場面：未持有看「上車」（回測月線有守＋帶量紅K＋法人轉買），持有中看「加碼」（底倉獲利 ≥ 3%＋帶量突破前高＋法人買超）；
  否決條件（跌破月線、爆量長黑、籌碼背離、融資追價、投信賣外資買…）任一成立為「不宜加碼」；大盤跌破下彎月線時降為觀察。
- 持有面：停損（破下彎月線、破月線＋法人賣、跌破關鍵價、保本線、硬停損 10%）、獲利出場、出貨警訊、獲利保護。
- 規則、參數都在 `settings.json`，可在網頁調整。

## 回溯分析

用歷史資料重播規則，檢查訊號出現後的實際走勢。最簡單的方式是在 repo 資料夾執行：

```bash
./deploy/backtest.sh                  # 補約兩年資料（第一次約 8～12 小時，可中斷續跑）→ 回溯分析 → 打開報表
./deploy/backtest.sh --skip-backfill  # 資料已補好，只重跑分析
./deploy/backtest.sh -- --grid stopLossPct=8,10,12   # 「--」之後的參數交給 twradar backtest
```

- 這台電腦已安裝雷達（有 `~/twradar-data`）：**共用雷達的資料庫**，抓一次、晨報與回溯都能用。
  雷達正在執行時，補抓交給雷達的背景工作（與每日排程排隊，不會同時對交易所發請求），腳本只顯示進度；
  Ctrl+C 只停止顯示，雷達會繼續補。
- 沒有安裝雷達，或加 `--separate`：用獨立資料夾 `~/twradar-backtest`，第一次會複製雷達的觀察清單與規則。
- 已安裝雷達時，可直接執行 `~/twradar-app/deploy/backtest.sh`（重跑 `deploy/install.sh` 會更新成最新版）。
也可以直接用指令：

```bash
cd ~/twradar-app
TWRADAR_HOME=~/twradar-data .venv/bin/python -m twradar backfill --days 560    # 先補約兩年全市場資料（約 8～12 小時，可中斷後續跑）
TWRADAR_HOME=~/twradar-data .venv/bin/python -m twradar backtest              # 產生報表
TWRADAR_HOME=~/twradar-data .venv/bin/python -m twradar backtest --grid profitTargetPct=8,10,15 --grid stopLossPct=8,10,12
```

- 結果在 `~/twradar-data/backtest/時間/`：`report.html`（用瀏覽器開）、`result.json`、`trades.csv`。
- 股票池預設 `auto`：每季依「當時」前 60 日成交金額排名（上市前 50＝大型、51～150＝中型、上櫃前 50＝上櫃）＋觀察清單，
  避免用今天的成份股回測過去。也可用 `--universe watchlist` 或 `--universe 檔案.csv`（每行：代號,組別）。
- 依大盤環境（多頭／盤整／空頭）、股票組、樣本內／樣本外、每半年分別統計；`--split` 指定樣本外起始日（預設後三分之一）。
- 訊號隔日開盤成交、扣費稅、使用還原股價。集保週資料沒有歷史，相關條件在回溯期間多為資料不足。

## 五、已知限制
- 證交所四項、櫃買中心三項來源皆已在實機確認可依日期抓取（2026/11）。程式會分辨「網站回傳較新日期（參數不對）」與「當日尚未公布」兩種情況，前者立即改用 FinMind 備援。
- 主力分點、買賣家數差沒有官方全市場資料，目前不抓，判定時該項顯示「?」。
- 集保股權分散表只提供最新一週，大戶／散戶週增減從安裝當週開始累積。
- 休市判斷：平日 17:00 後證交所仍無當日資料就視為休市。
- FinMind 免費額度有限；量大時可到 finmindtrade.com 申請 token，填入 `settings.json` 的 `sources.options.finmind.token`。

## 專案結構

```
twradar/
  providers/    資料源（可替換）：base.py 介面、twse.py 證交所、tpex.py 櫃買中心、finmind.py 逐檔備援
  dates.py      日期工具
  settings.py   使用者設定 settings.json：驗證、版本、遷移
  auth.py       權限：唯讀／管理者、密碼、session、Authenticator 介面
  config.py     系統設定 config.json
  net.py        HTTP 抓取：請求間隔、重試、封鎖偵測
  jobs.py       抓取流程、歷史補抓、背景工作佇列、每日排程
  analysis.py   指標計算（還原股價、均線、量能、法人與融資）與每日判定
  conditions.py 內建條件（上車、加碼、否決、停損、出貨警訊…）
  rules.py      規則引擎、預設規則、規則驗證與升級
  backtest.py   回溯分析；backtest_report.py 報表
  server.py     Web API 與靜態網頁
  db.py         SQLite 結構與存取
  web/          網頁 UI（原生 JS、SVG 圖表，無外部相依）
deploy/         一鍵安裝／移除腳本（macOS launchd、Linux systemd）
tests/          單元與端到端測試（內含模擬交易所伺服器）
```

## 開發與測試

```bash
python3 -m unittest discover -s tests -v   # 解析、判定、端到端（模擬交易所 → 抓取 → 分析 → API）
TWRADAR_HOME=./dev-data python3 -m twradar serve
docker compose up -d                        # 或用 Docker
```

歡迎發 Issue 或 Pull Request，尤其是櫃買中心格式的實測修正。

## 資料來源與使用規範

資料取自臺灣證券交易所、證券櫃檯買賣中心、臺灣集中保管結算所（集保戶股權分散表，每週）公開資訊，以及 FinMind（選用）。
集保開放資料只提供最新一週，系統從安裝當週開始每週累積，第二週起才有大戶／散戶週增減。請遵守各網站的使用條款，
保持低請求頻率，勿將本程式用於大量或高頻抓取。

## 授權

[MIT](LICENSE) © 2026 Paul

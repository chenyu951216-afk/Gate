# gate-quant-ranking-scanner

Gate USDT 永續合約量化找幣、排名、自動下單與持倉管理系統。它保留原本的 30 分鐘找幣邏輯，符合排名條件後才交給交易層執行；交易層負責防重複建倉、最高槓桿、名目金額、RR、分層止損止盈與持續移動管理。回測系統保留但暫不介入實盤下單流程。

## 功能

- Gate 官方 Futures REST v4 與 Futures WebSocket adapter。
- 4H 大方向、30m 正式排名、15m/5m 回踩輔助。
- EMA、BOLL、VWAP、DMI、ADX、MFI、ATR、成交額、OI、基差、funding、結構與突破。
- 資料缺口、單位轉換、暖機、時間對齊、API partial failure 與 stale data diagnostics。
- 每根 30m 收線後排程掃描，預設收線後 20 秒，防止重複執行。
- 響應式繁體中文深色網頁、Discord 完整 1～10 名分段通知。
- Gate 私有 Futures REST v4：持倉同步、最大槓桿偵測、IOC 市價下單、條件止損止盈與保護單重掛。
- 下單通知與找幣通知使用不同 Discord webhook；同幣重複掃描會被交易層以實際持倉／未成交單防重。
- 持倉管理每 5 秒獨立追蹤，不依賴 30 分鐘掃描；網站可暫停／恢復新下單，暫停不會停止既有持倉保護。
- 歷史 replay job、進度、取消、JSON/CSV/HTML export。
- 下一根開盤／固定持有／ATR 止損止盈／手續費／滑價／MFE/MAE／walk-forward 回測引擎。
- PostgreSQL async mode；沒有 `DATABASE_URL` 時明確使用 memory mode，重啟資料會消失。

## Gate 資料來源

本專案只使用官方 endpoint：`https://api.gateio.ws/api/v4/futures/usdt`。行情、持倉、訂單、槓桿與 price-triggered orders 都由 Gate REST v4 提供；完整欄位、單位與歷史限制見 [docs/gate-data-map.md](docs/gate-data-map.md)。Gate 官方文件：[REST Futures API](https://www.gate.com/docs/developers/apiv4/en/futures/)、[Futures WebSocket](https://www.gate.com/docs/developers/futures/ws/)。

官方 API 的限制很重要：K 線單次最多 2000 根；逐筆成交的正負 size 可作為即時 taker side；歷史 spread、任意歷史 24h ticker 與完整歷史 active flow 不一定可由官方公開接口重建。系統會顯示 `unavailable`、扣除不可用權重與降低完整度，不會用目前數字或 0 偽造歷史狀態。

## 本機安裝

需要 Python 3.12。建立 venv 後執行 `python -m pip install -r requirements.txt`，複製 `.env.example` 為 `.env`，再執行 `python main.py`。服務會監聽 `0.0.0.0:${PORT:-8080}`；`GET /health` 與 `GET /api/rankings` 可用來檢查。

## Docker

執行 `docker compose up --build`。Dockerfile 使用 Python 3.12 slim、非 root `app` 使用者、單一 Uvicorn worker、`/health` healthcheck，且不複製 `.env`。

## GitHub

網頁一鍵上傳時，把根目錄內容完整上傳，確認 `Dockerfile`、`main.py`、`.github/workflows/` 在根目錄，且 `.env` 沒有被加入。Git 指令依序為 `git init`、`git add .`、`git commit -m "build gate quant ranking scanner"`、`git branch -M main`、`git remote add origin https://github.com/<user>/<repo>.git`、`git push -u origin main`。

## 自動下單與持倉管理

預設 `AUTO_ORDER_ENABLED=false`、`POSITION_MANAGEMENT_ENABLED=false`，先完成 API key、PostgreSQL 與 webhook 設定，再明確設為 `true`。啟用後流程如下：

1. 每 30 分鐘掃描一次，沿用既有找幣與排名邏輯。
2. 交易層先查 Gate 實際持倉與 open orders；同一合約已有持倉或未成交開倉單時只通知跳過，不重複下單。
3. `BTC_USDT,ETH_USDT,SOL_USDT,BNB_USDT,HYPE_USDT` 合計最多同時持倉 2 種；BTC/ETH 名目預設 40,000 USDT，其他主要幣預設 20,000 USDT，一般山寨預設 2,000 USDT。
4. 先從 Gate contract `leverage_max` 取得最高槓桿，缺少時再查 risk-limit tiers；無法確認最高槓桿時拒絕下單。下單前強制設定 isolated 最高槓桿。
5. 市價 IOC 成交後，先掛完整止損，再掛 TP1 25%、TP2 30%、TP3 25%，剩餘 20% 由 Runner 移動止損管理。任何保護單安裝失敗會嘗試緊急 reduce-only 平倉。
6. 持倉管理背景任務每 5 秒同步，使用已收線 15m/5m K 線與 ATR；止損只往有利方向移動，達到 1R 才允許保本，2R 後才啟用結構移動，2.5R 後加速保護。

主要控制 API：`GET /api/trading/status`、`GET /api/trading/positions`、`POST /api/trading/pause`、`POST /api/trading/resume`、`POST /api/trading/manage-once`。pause 只停止新建倉，不會撤掉既有止損止盈。

交易與通知金鑰必須放在環境變數，不要提交 `.env`。建議 Gate API key 只開 Futures 交易權限，不開提領權限。

## Zeabur

從 GitHub 建立 service，使用根目錄 Dockerfile；加入 PostgreSQL service 並把 `DATABASE_URL` 設到 app。保持 `PORT=8080`，部署完成後開啟 `/health`。不要設定多個 worker，避免 scheduler 與持倉管理重複執行。

可直接貼入 Zeabur 的環境變數（請先替換 secrets）：

`APP_NAME=gate-quant-ranking-scanner`

`APP_ENV=production`

`LOG_LEVEL=INFO`

`HOST=0.0.0.0`

`PORT=8080`

`TIMEZONE=Asia/Taipei`

`DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@HOST:5432/DBNAME`

`GATE_REST_BASE_URL=https://api.gateio.ws/api/v4`

`GATE_WS_URL=wss://fx-ws.gateio.ws/v4/ws/usdt`

`GATE_API_KEY=REPLACE_WITH_GATE_FUTURES_KEY`

`GATE_API_SECRET=REPLACE_WITH_GATE_SECRET`

`GATE_MARGIN_MODE=isolated`

`GATE_MARKET_ORDER_SLIP_RATIO=0.03`

`MIN_24H_TURNOVER_USDT=7000000`

`MAX_SPREAD_PCT=0.10`

`MIN_30M_CANDLES=240`

`MIN_4H_CANDLES=150`

`MIN_DATA_COMPLETENESS_PCT=70`

`RANKING_MIN_SCORE=55`

`SCAN_DELAY_SECONDS=20`

`SCAN_ON_STARTUP=false`

`SCHEDULER_ENABLED=true`

`AUTO_ORDER_ENABLED=false`

`POSITION_MANAGEMENT_ENABLED=false`

`POSITION_MANAGER_INTERVAL_SECONDS=5`

`POSITION_MARKET_REFRESH_SECONDS=15`

`MAX_MARKET_DRIVER_POSITIONS=2`

`MARKET_DRIVER_CONTRACTS=BTC_USDT,ETH_USDT,SOL_USDT,BNB_USDT,HYPE_USDT`

`REGULAR_ALT_NOTIONAL_USDT=2000`

`MARKET_DRIVER_NOTIONAL_USDT=20000`

`BTC_ETH_NOTIONAL_USDT=40000`

`MINIMUM_ORDER_RR=1.0`

`REQUIRE_MAX_LEVERAGE=true`

`TRADING_CONTROL_TOKEN=REPLACE_WITH_TRADING_CONTROL_TOKEN`

`MANUAL_SCAN_TOKEN=REPLACE_WITH_LONG_RANDOM_VALUE`

`ADMIN_BEARER_TOKEN=REPLACE_WITH_LONG_RANDOM_VALUE`

`DISCORD_WEBHOOK_URL=`

`SCAN_DISCORD_WEBHOOK_URL=`

`ORDER_DISCORD_WEBHOOK_URL=`

`PUBLIC_BASE_URL=https://YOUR_ZEABUR_DOMAIN`

`REPLAY_REQUIRE_HISTORICAL_SPREAD=true`

`REPLAY_REQUIRE_HISTORICAL_ACTIVE_FLOW=false`

## 即時掃描與 Discord

手動掃描使用 `POST /api/scan` 並帶 `Authorization: Bearer $ADMIN_BEARER_TOKEN`；body 可用 `{"dry_run":true,"top_n":10,"notify_discord":false}`。Discord 測試使用 `POST /api/notifications/test`。找幣通知使用 `SCAN_DISCORD_WEBHOOK_URL`，下單與持倉通知使用 `ORDER_DISCORD_WEBHOOK_URL`；兩者分流且不共用防重。超過 Discord 限制會拆段並標示序號，429 會依 `retry_after` 重試。

## 歷史重播

網頁 `/replay` 可輸入 `2026-06-09 10:00` 到 `2026-06-09 12:00`、時區 `Asia/Taipei`、30 分鐘間隔。API 建立背景工作，結果從 `/api/replay/{job_id}/results`、`diagnostics` 與 `export.json/csv/html` 取得。無可靠排名不是系統錯誤：代表資料完整度、時間對齊或官方歷史資料可得性未達門檻。

## 回測

先完成 replay，再把 `replay_job_id` POST 到 `/api/backtest`。回測才會計算進場、持有、ATR 止損止盈、費用、滑價、MFE/MAE 與 walk-forward；replay 本身不假設下單。

## API

`GET /`、`GET /health`、`GET /api/status`、`GET /api/scan/latest`、`GET /api/rankings`、`GET /api/rankings/long`、`GET /api/rankings/short`、`GET /api/rankings/history`、`GET /api/contracts/{contract}`、`GET /api/contracts/{contract}/history`、`POST /api/scan`、`POST /api/notifications/test`、`GET /api/notifications/history`、`GET /api/trading/status`、`GET /api/trading/positions`、`POST /api/trading/pause`、`POST /api/trading/resume`、`POST /api/trading/manage-once`、replay 與 backtest API 都有 OpenAPI 文件 `/docs`。

## 測試與限制

執行 `pytest -q`、`ruff check app tests scripts main.py`、`mypy app` 與 `docker build -t gate-quant-ranking-scanner .`。指標只使用目前及之前資料，正式排名只用已收線 K，rolling 結構不引用未來擺動點。任何 API timeout、429、schema 異常、資料缺口或指標錯誤都會進入 diagnostics。啟用實盤前先用 Gate 子帳戶／低風險 API key 驗證最小額度、槓桿回傳、price order 欄位與實際成交；本專案不保證交易所、網路或清算風險，排名第一也不代表一定上漲或獲利。

# Zeabur 部署

1. 將 repository 上傳 GitHub。
2. Zeabur 建立 GitHub service，使用根目錄 Dockerfile。
3. 加入 PostgreSQL service，把連線字串填入 `DATABASE_URL`；程式會在資料庫剛啟動尚未可連線時重試 12 次，每次間隔 5 秒。
4. 設定 `PORT=8080`、`HOST=0.0.0.0`，只使用一個 Uvicorn worker。
5. 設定 `ADMIN_BEARER_TOKEN`、`TRADING_CONTROL_TOKEN`；Discord 與 Bitget Key 沒填時，找幣通知與自動交易會停用。
6. 要啟用實盤，明確設定 `AUTO_ORDER_ENABLED=true` 與 `POSITION_MANAGEMENT_ENABLED=true`，並填入 `BITGET_API_KEY`、`BITGET_API_SECRET`、`BITGET_API_PASSPHRASE`、`SCAN_DISCORD_WEBHOOK_URL`、`ORDER_DISCORD_WEBHOOK_URL`。兩個 webhook 不要填同一個群組；`BITGET_MARGIN_MODE=crossed`、`BITGET_POSITION_MODE=one_way_mode`、`ENTRY_ORDER_MODE=limit`、`MINIMUM_ORDER_RR=1.2`、`RISK_PER_TRADE_PCT=0.01`、`MINIMUM_VIABLE_POSITION_EQUITY_MULTIPLE=0.25`、`MAX_PROMOTED_RISK_PER_TRADE_PCT=0.025`。
   Gate 只負責掃描與排行；Bitget 下單前會強制嘗試 one-way/crossed 並回讀確認，無法確認就拒絕新單。既有持倉不會被自動平倉或強制轉換。
   所有幣種均使用動態風險倉位；原本最多 2 個大盤驅動持倉仍由 `MARKET_DRIVER_CONTRACTS` 控制。
7. 若使用 CoinGlass 清算資料，設定 `COINGLASS_ENABLED=true`、`COINGLASS_API_KEY`；清算資料只作為止損止盈與持倉管理參考，不會阻止原始找幣排名。`COINGLASS_USE_HEATMAP=true` 會優先讀取清算熱圖，熱圖方案不可用時仍可用 30m 聚合清算歷史作備援。若要強制必須有熱圖，才把 `COINGLASS_REQUIRE_HEATMAP=true`，但需購買支援該 endpoint 的 CoinGlass 方案。
8. 維持單一 Uvicorn worker；持倉管理與掃描都是背景任務，多 worker 會造成重複執行。
9. 部署後開啟 `/health` 與 `/api/trading/status`；`/api/status` 會顯示排程是否運作、上次掃描結果與下次 30 分鐘掃描時間。以管理 Token 測試 `/api/trading/overview` 與 `/api/trading/positions`；先用子帳戶／最小額度確認 Bitget 的槓桿、合約數量與保護單回傳。

初始止損依 30m 找幣論點、15m 戰術結構、最近 144 根 30m K 的抗離群波動基準、最近 6h／2h 制度切換與 CoinGlass 反向流動性池共同計算。交易層按帳戶淨值風險與止損距離反推倉位，並套用單筆、全組合及可用保證金上限；CoinGlass 不會阻止找幣排名，也不能單獨創造止損，只在清算池與既有結構失效區重疊時提供外移緩衝。

限價單使用已收 5m 的 EMA20／EMA50、VWAP、局部高低點與 ATR 距離找價，最大追價／回撤範圍受 `FIVE_MINUTE_ENTRY_MAX_ATR` 與 `FIVE_MINUTE_ENTRY_MAX_DISTANCE_PCT` 共同限制，最多等待 10,800 秒（3 小時）。股票掛單在標的 regular session 收盤時撤銷。成交後持倉監控每次取得至少 288 根已收 15m 與 96 根已收 5m K，資料有缺口、重複、錯位或過期時只修復交易所保護，不進行趨勢退出。15m 多因子確認後才開啟 5m 出場；Bitget 原始止損在等待回彈時不會移遠。Zeabur 新增環境變數可直接由 `.env.example` 複製。

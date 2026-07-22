# Zeabur 部署

1. 將 repository 上傳 GitHub。
2. Zeabur 建立 GitHub service，使用根目錄 Dockerfile。
3. 加入 PostgreSQL service，把連線字串填入 `DATABASE_URL`；程式會在資料庫剛啟動尚未可連線時重試 12 次，每次間隔 5 秒。
4. 設定 `PORT=8080`、`HOST=0.0.0.0`，只使用一個 Uvicorn worker。
5. 設定 `ADMIN_BEARER_TOKEN`、`TRADING_CONTROL_TOKEN`；Discord 與 Gate Key 沒填時，找幣通知與自動交易會停用。
6. 要啟用實盤，明確設定 `AUTO_ORDER_ENABLED=true` 與 `POSITION_MANAGEMENT_ENABLED=true`，並填入 `GATE_API_KEY`、`GATE_API_SECRET`、`SCAN_DISCORD_WEBHOOK_URL`、`ORDER_DISCORD_WEBHOOK_URL`。兩個 webhook 不要填同一個群組；`GATE_MARGIN_MODE=cross`、`ENTRY_ORDER_MODE=limit`、`MINIMUM_ORDER_RR=1.0`、`MAX_INITIAL_STOP_LOSS_USDT=1000`、`MAX_SAME_DIRECTION_ORDERS_PER_BATCH=2`。
   程式會強制把 `GATE_MARGIN_MODE` 正規化為 `cross`；若 Gate API 回讀仍不是 cross，該筆新單會被拒絕，不會讓它以逐倉模式成交。
   ZEC 已加入 `MARKET_DRIVER_NOTIONAL_CONTRACTS`，使用 20,000U 名目；原本最多 2 個大盤驅動持倉仍由 `MARKET_DRIVER_CONTRACTS` 控制。
7. 若使用 CoinGlass 清算資料，設定 `COINGLASS_ENABLED=true`、`COINGLASS_API_KEY`；清算資料只作為止損止盈與持倉管理參考，不會阻止原始找幣排名。`COINGLASS_USE_HEATMAP=true` 會優先讀取清算熱圖，熱圖方案不可用時仍可用 30m 聚合清算歷史作備援。若要強制必須有熱圖，才把 `COINGLASS_REQUIRE_HEATMAP=true`，但需購買支援該 endpoint 的 CoinGlass 方案。
8. 維持單一 Uvicorn worker；持倉管理與掃描都是背景任務，多 worker 會造成重複執行。
9. 部署後開啟 `/health` 與 `/api/trading/status`；`/api/status` 會顯示排程是否運作、上次掃描結果與下次 30 分鐘掃描時間。以管理 Token 測試 `/api/trading/overview` 與 `/api/trading/positions`；先用子帳戶／最小額度確認 Gate 的槓桿、合約數量與 price order 回傳。

初始止損依原找幣週期的結構與 ATR buffer 計算，但不再因 ATR 距離超過固定值直接拒單；交易層改用實際持倉名目金額計算止損損失，超過 1,000 USDT 才拒絕。CoinGlass 不參與此拒單判斷，也不會阻止找幣排名。

限價單使用接近買一／賣一的價格，最多等待 180 秒；價格朝交易方向突破 0.5% 時會撤銷未成交掛單。掛單成交後由持倉管理接管並補掛完整保護單。

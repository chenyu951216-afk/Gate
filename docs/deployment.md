# Zeabur 部署

1. 將 repository 上傳 GitHub。
2. Zeabur 建立 GitHub service，使用根目錄 Dockerfile。
3. 加入 PostgreSQL service，把連線字串填入 `DATABASE_URL`；程式會在資料庫剛啟動尚未可連線時重試 12 次，每次間隔 5 秒。
4. 設定 `PORT=8080`、`HOST=0.0.0.0`，只使用一個 Uvicorn worker。
5. 設定 `ADMIN_BEARER_TOKEN`、`TRADING_CONTROL_TOKEN`；Discord 與 Gate Key 沒填時，找幣通知與自動交易會停用。
6. 要啟用實盤，明確設定 `AUTO_ORDER_ENABLED=true` 與 `POSITION_MANAGEMENT_ENABLED=true`，並填入 `GATE_API_KEY`、`GATE_API_SECRET`、`SCAN_DISCORD_WEBHOOK_URL`、`ORDER_DISCORD_WEBHOOK_URL`。兩個 webhook 不要填同一個群組。
7. 若使用 CoinGlass 清算資料，設定 `COINGLASS_ENABLED=true`、`COINGLASS_API_KEY`；清算資料只作為止損止盈與持倉管理參考，不會阻止原始找幣排名。`COINGLASS_USE_HEATMAP=true` 會優先讀取清算熱圖，熱圖方案不可用時仍可用 30m 聚合清算歷史作備援。若要強制必須有熱圖，才把 `COINGLASS_REQUIRE_HEATMAP=true`，但需購買支援該 endpoint 的 CoinGlass 方案。
8. 維持單一 Uvicorn worker；持倉管理與掃描都是背景任務，多 worker 會造成重複執行。
9. 部署後開啟 `/health` 與 `/api/trading/status`；`/api/status` 會顯示排程是否運作、上次掃描結果與下次 30 分鐘掃描時間。以管理 Token 測試 `/api/trading/overview` 與 `/api/trading/positions`；先用子帳戶／最小額度確認 Gate 的槓桿、合約數量與 price order 回傳。

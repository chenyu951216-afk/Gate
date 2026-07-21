# Zeabur 部署

1. 將 repository 上傳 GitHub。
2. Zeabur 建立 GitHub service，使用根目錄 Dockerfile。
3. 加入 PostgreSQL service，把連線字串填入 `DATABASE_URL`。
4. 設定 `PORT=8080`、`HOST=0.0.0.0`，只使用一個 Uvicorn worker。
5. 設定 `ADMIN_BEARER_TOKEN`、`TRADING_CONTROL_TOKEN`；Discord 與 Gate Key 沒填時，找幣通知與自動交易會停用。
6. 要啟用實盤，明確設定 `AUTO_ORDER_ENABLED=true` 與 `POSITION_MANAGEMENT_ENABLED=true`，並填入 `GATE_API_KEY`、`GATE_API_SECRET`、`SCAN_DISCORD_WEBHOOK_URL`、`ORDER_DISCORD_WEBHOOK_URL`。兩個 webhook 不要填同一個群組。
7. 維持單一 Uvicorn worker；持倉管理與掃描都是背景任務，多 worker 會造成重複執行。
8. 部署後開啟 `/health` 與 `/api/trading/status`，以管理 Token 測試 `/api/trading/positions`；先用子帳戶／最小額度確認 Gate 的槓桿、合約數量與 price order 回傳。

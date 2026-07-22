# 常見問題

- `/health` 可開啟但沒有排名：服務健康只代表 API process 正常；排名仍可能因 Gate API、流動性、K 線暖機或完整度不合格而為空。
- replay 沒有可靠排名：先看 `/api/replay/{job_id}/diagnostics`。歷史 spread／active flow 官方不可重建時，strict replay 會抑制排名，這是安全行為。
- Discord disabled：確認 `DISCORD_WEBHOOK_URL`，並使用 `POST /api/notifications/test`。
- `INVALID_SLIP_RATIO`：舊版市價參數超過 Gate 合法範圍；新版進場固定使用 `ENTRY_ORDER_MODE=limit`，且不會把 `market_order_slip_ratio` 傳給限價單。重新部署後保留 `GATE_MARKET_ORDER_SLIP_RATIO=0.01`。
- 限價單一直未成交：查看下單通知的 `entry_limit_price` 與 `entry_order_id`；價格朝訊號方向移動超過 `LIMIT_ENTRY_CANCEL_MOVE_PCT` 會撤單，最久等待 `LIMIT_ENTRY_TIMEOUT_SECONDS`。
- 成交後沒有保護單：查看下單事件／下單通知中的 `PROTECTION_ORDER_FAILED` 或 `PROTECTION_ORDER_NOT_CONFIRMED` 詳細錯誤；系統會清理不完整的保護單、持續重掛交易所保護，並只在觸及止損／止盈價位時送出 reduce-only 後台備援，避免裸倉。
- 服務重啟資料消失：未設定 `DATABASE_URL` 時是 memory mode；Zeabur 正式環境應使用 PostgreSQL。
- Docker healthcheck 失敗：確認 container 內監聽 `0.0.0.0`，且 Zeabur 的 `PORT` 沒有被覆蓋成空值。

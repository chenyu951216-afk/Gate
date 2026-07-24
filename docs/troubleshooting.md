# 常見問題

- `/health` 可開啟但沒有排名：服務健康只代表 API process 正常；排名仍可能因 Gate API、流動性、K 線暖機或完整度不合格而為空。
- replay 沒有可靠排名：先看 `/api/replay/{job_id}/diagnostics`。歷史 spread／active flow 官方不可重建時，strict replay 會抑制排名，這是安全行為。
- Discord disabled：確認 `DISCORD_WEBHOOK_URL`，並使用 `POST /api/notifications/test`。
- `INVALID_SLIP_RATIO`：舊版市價參數超過交易所合法範圍；目前進場固定使用 `ENTRY_ORDER_MODE=limit`，不會把市價滑價參數傳給限價單。
- 顯示逐倉：確認已重新部署最新程式；新單前會依序嘗試 Bitget `set-margin-mode`、`set-leverage` 與 `set-position-mode`，並驗證 `marginMode=crossed`、`posMode=one_way_mode`。若帳戶仍有無法切換的既有逐倉／雙向持倉，系統會拒絕新單，避免誤開不相容模式。
- 限價單一直未成交：後台每 5 秒持續讀取 Bitget 掛單與持倉；查看下單通知的 `entry_limit_price` 與 `entry_order_id`。價格朝訊號方向移動超過 `LIMIT_ENTRY_CANCEL_MOVE_PCT` 會提前撤單，否則最久等待 `LIMIT_ENTRY_TIMEOUT_SECONDS=10800`（3 小時）。
- 方向反轉未換倉：同一幣種的新方向訊號會先撤 Bitget 保護單與舊限價單，確認舊持倉為零後才送出新方向限價單；若撤單或平倉未獲交易所確認，系統會拒絕反手，避免新舊方向同時存在。
- 正式模式掃描但沒有下單：先看 `/api/scan/latest` 的 `trading.orders`。若 CoinGlass 沒有可用熱圖或目標價不滿足 RR，系統會回退到本策略的 R 倍數 TP；TP1 仍必須達到 `MINIMUM_ORDER_RR=1.0`，並不會因正式模式而放寬。
- 成交後沒有保護單：查看下單事件／下單通知中的 `PROTECTION_ORDER_FAILED` 或 `PROTECTION_ORDER_NOT_CONFIRMED` 詳細錯誤；系統會清理不完整的保護單、持續重掛交易所保護，並只在觸及止損／止盈價位時送出 reduce-only 後台備援，避免裸倉。
- 服務重啟資料消失：未設定 `DATABASE_URL` 時是 memory mode；Zeabur 正式環境應使用 PostgreSQL。
- Docker healthcheck 失敗：確認 container 內監聽 `0.0.0.0`，且 Zeabur 的 `PORT` 沒有被覆蓋成空值。

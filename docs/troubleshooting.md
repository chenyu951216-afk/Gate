# 常見問題

- `/health` 可開啟但沒有排名：服務健康只代表 API process 正常；排名仍可能因 Gate API、流動性、K 線暖機或完整度不合格而為空。
- replay 沒有可靠排名：先看 `/api/replay/{job_id}/diagnostics`。歷史 spread／active flow 官方不可重建時，strict replay 會抑制排名，這是安全行為。
- Discord disabled：確認 `DISCORD_WEBHOOK_URL`，並使用 `POST /api/notifications/test`。
- 服務重啟資料消失：未設定 `DATABASE_URL` 時是 memory mode；Zeabur 正式環境應使用 PostgreSQL。
- Docker healthcheck 失敗：確認 container 內監聽 `0.0.0.0`，且 Zeabur 的 `PORT` 沒有被覆蓋成空值。


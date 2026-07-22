# 歷史重播與績效回測

歷史重播回答「當時會排出什麼」，不模擬下單；績效回測回答「依排名進場後結果如何」。回放的每個時間點都以 `T` 作為資料上限，使用向前暖機的 4H/30m/15m/5m K 線、歷史 OI 與 funding。歷史 spread、歷史 24h ticker、歷史逐筆 taker flow 若官方沒有可重建 API，會保留 diagnostics 並不以現在資料補洞。

重播 API：`POST /api/replay`、`GET /api/replay/{job_id}/status`、`GET /api/replay/{job_id}/results`、`GET /api/replay/{job_id}/diagnostics`、`GET /api/replay/{job_id}/export.json`、`export.csv`、`export.html`、`POST /api/replay/{job_id}/cancel`。


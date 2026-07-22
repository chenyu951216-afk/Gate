# 架構

服務由 FastAPI、Gate 官方 REST/WebSocket adapter、純 pandas 指標、scanner、trading execution/position manager、replay、backtest、Discord delivery 與 SQLAlchemy repository 組成。

即時掃描流程：合約與 ticker 低成本篩選 → K 線／OI／funding 收集 → 指標與結構 → 連續評分 → 風險扣分 → 合格標的排名 → 儲存與通知。

交易流程：合格排名 → 查 Gate 實際持倉與 open orders → 主要幣 2 種上限／同幣防重／名目金額 → risk-limit tier 最高槓桿 → 市價 IOC → 完整止損 → TP1/TP2/TP3 → PostgreSQL 管理狀態。持倉管理是獨立 5 秒循環，不等待 30m 掃描；pause 只阻止新建倉，不停止既有保護。

歷史重播流程：產生 30m timeline → 以每個時間點為上限取得暖機資料 → 依上市時間過濾 universe → 使用同一套 analyzer/scoring → 輸出 ranking 與 diagnostics。所有回放資料都會以 UTC 儲存並以 Asia/Taipei 顯示。

# 架構

服務由 FastAPI、Gate 掃描 REST/WebSocket adapter、Bitget 執行 REST adapter、純 pandas 指標、scanner、trading execution/position manager、replay、backtest、Discord delivery 與 SQLAlchemy repository 組成。

即時掃描流程：合約與 ticker 低成本篩選 → 4h／30m／15m／5m K 線、OI、funding 收集 → 72h 基準＋6h／2h 波動制度、指標與結構 → 30m 主導的連續評分 → 風險扣分 → 合格標的排名 → 儲存與通知。

交易流程：Gate 合格排名 → 查 Bitget 實際持倉與 open orders → 主要幣 2 種上限／同幣防重 → 依每幣執行品質、共用波動尺度、止損距離、帳戶風險與組合容量計算名目金額 → Bitget `maxLever` → crossed/one-way 限價單（進場單先綁初始止損）→ 回讀確認 Bitget 止損與 TP1/TP2/TP3 → PostgreSQL 管理狀態。持倉管理是獨立 5 秒循環，但只以已收 30m K 判定交易論點失效；15m 作確認、5m 只作警示。同一根 30m K 不會因每 5 秒輪詢而重複累積反轉。交易所保護優先，只有交易所保護失敗且觸價時才送後台 reduce-only 備援；pause 只阻止新建倉，不停止既有保護。

歷史重播流程：產生 30m timeline → 以每個時間點為上限取得暖機資料 → 依上市時間過濾 universe → 使用同一套 analyzer/scoring → 輸出 ranking 與 diagnostics。所有回放資料都會以 UTC 儲存並以 Asia/Taipei 顯示。

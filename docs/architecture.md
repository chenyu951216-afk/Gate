# 架構

服務由 FastAPI、Gate 掃描 REST/WebSocket adapter、Bitget 執行 REST adapter、純 pandas 指標、scanner、trading execution/position manager、replay、backtest、Discord delivery 與 SQLAlchemy repository 組成。

即時掃描流程：合約與 ticker 以 500 萬 USDT 成交額及價差初篩 → 股票標的交易日曆 gate → 4h／30m／15m／5m K 線、OI、funding 收集 → 30m／4h 完整性驗證與必要時補抓 → 72h 基準＋6h／2h 波動制度 → 依該幣制度選擇突破／量能／ADX 門檻 → 4h/30m 趨勢排名或嚴格的逆 4h、30m 短線排名 → 訊號生命週期分層 → 儲存、下單與通知。生命週期只改通知標示，首次進榜仍立即進入下單流程。

交易流程：Gate 合格排名 → 查 Bitget 實際持倉與 open orders → 主要幣 2 種上限／同幣防重 → 以已收 5m EMA／VWAP／結構及 ATR 距離選擇限價 → 依每幣執行品質、共用波動尺度、止損距離、帳戶風險與組合容量計算名目金額 → Bitget `maxLever` → crossed/one-way 限價單（進場單先綁初始止損）→ 回讀確認 Bitget 止損與 TP1/TP2/TP3 → PostgreSQL 管理狀態。持倉後另走獨立狀態機：每 5 秒同步持倉與保護單，但只有新的已收 15m K 才能累積結構、EMA、DMI/ADX、VWAP、MFI 與量能反轉證據；確認趨勢消失後進入 EXIT_ARMED，5m 只能選擇退出時機，不能自行宣告趨勢反轉。時鐘到期、單根 5m 雜訊與即時 mark 插針都不能直接關倉。交易所原始止損仍為最終風險邊界。

歷史重播流程：產生 30m timeline → 以每個時間點為上限取得暖機資料 → 依上市時間過濾 universe → 使用同一套 analyzer/scoring → 輸出 ranking 與 diagnostics。所有回放資料都會以 UTC 儲存並以 Asia/Taipei 顯示。

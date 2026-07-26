# Gate 資料來源對照表

以下為官方 API v4 的實際使用對照；Base URL 為 `https://api.gateio.ws/api/v4`，settle 為 `usdt`。

| 資料 | 官方 endpoint | 實際欄位 | 原始／標準單位 | 更新頻率 | 公開／Key | 歷史保留與限制 |
|---|---|---|---|---|---|---|
| 交易中合約 | `GET /futures/usdt/contracts` | `name,status,quanto_multiplier,launch_time,delisting_time` | 秒；multiplier 原始字串，程式轉 float | 每輪 | 公開／否 | 目前交易中清單 |
| 全部合約 | `GET /futures/usdt/contracts_all` | 同上 | 秒、合約乘數 | 每個 replay timepoint | 公開／否 | 用於排除尚未上市／已下架 |
| ticker | `GET /futures/usdt/tickers` | `last,change_percentage,volume_24h_quote,mark_price,index_price,funding_rate,highest_bid,lowest_ask` | quote turnover 為 USDT；價格為報價貨幣 | 即時 | 公開／否 | 沒有官方歷史 ticker snapshot |
| K 線 | `GET /futures/usdt/candlesticks` | `t,v,o,h,l,c,sum`；時間 `t` | 秒；`v` 合約張數，`sum` amount／成交額；程式優先使用 `sum` | 5m/15m/30m/4h | 公開／否 | 單次最多 2000 根，需依時間切段 |
| mark K 線 | candlestick `contract=mark_<name>` | `t,o,h,l,c` | 價格 | API | 公開／否 | 可取得時使用 |
| index K 線 | candlestick `contract=index_<name>` | `t,o,h,l,c` | 價格 | API | 公開／否 | 可取得時使用 |
| funding | `GET /futures/usdt/funding_rate` | `t,r` | 秒、比例 | funding interval | 公開／否 | 有歷史查詢，回放只取 `t <= T` |
| OI／統計 | `GET /futures/usdt/contract_stats` | `time,open_interest,open_interest_usd,mark_price,long_liq_*,short_liq_*` | OI 是合約張數；USD 欄位按官方欄位 | API interval | 公開／否 | 歷史範圍與 limit 依 Gate 回應 |
| 即時成交 | REST `GET /futures/usdt/trades`；WS `futures.trades` | `size,price,create_time`; 正 size taker buyer、負 size taker seller | size 為合約張數 | 即時 | 公開／否 | REST 沒有可可靠重建任意歷史區間的時間查詢，故 replay 標記 unavailable |
| BBO | REST `order_book`；WS `futures.book_ticker` | bid／ask | 價格 | 即時 | 公開／否 | 歷史 spread 沒有官方保存 snapshot，replay 不使用目前 spread |
| 清算 | `GET /futures/usdt/liq_orders`；WS public liquidates | `time,contract,order_price,fill_price,left` 等 | 訂單大小／價格 | 即時／有限時間窗 | REST 需 Key；部分欄位 public 不返回 | 官方要求 `from/to` 最大 3600 秒，回放缺資料標記 unavailable |

不可取得的指標不會用 0 取代；該權重會移除，完整度降低，若低於門檻就不入榜。Gate App 與本專案可能因 EMA warm-up、Wilder smoothing、K 線時區、資料截斷與缺口處理不同而有小幅差異。


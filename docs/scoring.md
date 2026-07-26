# 評分

做多與做空使用完全對稱的 100 點權重：4H 環境 12、30m 突破 18、結構 8、成交額 10、OI 10、DMI 5、ADX 7、MFI 5、EMA 5、VWAP 4、BOLL 4、主動流 6、15m/5m 回踩 6。

缺失項目會移除該項權重，使用 `raw_score / available_weight * 100` 正規化；`data_completeness_pct` 是可用權重比例，不是勝率。綜合分數為 `primary*0.72 + direction_edge*0.13 + completeness*0.10 + liquidity*0.05 - risk_penalty`，最後限制在 0 到 100。


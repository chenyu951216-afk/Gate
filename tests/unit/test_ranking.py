from app.scanner.ranking import build_rankings

def test_top_n_does_not_fill_missing_names():
    item = {"contract":"BTC_USDT","ranking_score":80,"bull_score":80,"bear_score":40,"qualifies":True}; result = build_rankings([item], 10); assert len(result["combined"]) == 1; assert result["combined"][0]["rank"] == 1


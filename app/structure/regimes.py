def regime_state(
    ema20: float | None,
    ema50: float | None,
    ema200: float | None,
    adx: float | None,
    boll_bandwidth_percentile: float | None,
) -> str:
    if None in (ema20, ema50, ema200, adx):
        return "mixed"
    assert ema20 is not None and ema50 is not None and ema200 is not None and adx is not None
    if boll_bandwidth_percentile is not None and boll_bandwidth_percentile < 20 and adx < 18:
        return "low_volatility"
    if boll_bandwidth_percentile is not None and boll_bandwidth_percentile > 80 and adx > 30:
        return "high_volatility"
    if ema20 > ema50 > ema200 and adx >= 18:
        return "bullish"
    if ema20 < ema50 < ema200 and adx >= 18:
        return "bearish"
    if adx < 18:
        return "range"
    return "mixed"

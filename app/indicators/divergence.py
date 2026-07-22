import pandas as pd


def divergence(price: pd.Series, oscillator: pd.Series, lookback: int = 20) -> dict[str, bool]:
    if len(price.dropna()) < lookback or len(oscillator.dropna()) < lookback:
        return {"bearish": False, "bullish": False, "available": False}
    p = price.dropna().iloc[-lookback:]
    o = oscillator.reindex(p.index).dropna()
    if len(o) < lookback // 2:
        return {"bearish": False, "bullish": False, "available": False}
    p_change = float(p.iloc[-1] - p.iloc[0])
    o_change = float(o.iloc[-1] - o.iloc[0])
    return {
        "bearish": p_change > 0 and o_change < 0,
        "bullish": p_change < 0 and o_change > 0,
        "available": True,
    }


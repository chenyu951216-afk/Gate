import pandas as pd


def range_features(high: pd.Series, low: pd.Series, close: pd.Series, window: int = 20) -> dict[str, float | bool | None]:
    highs = high.astype(float).iloc[-window:]
    lows = low.astype(float).iloc[-window:]
    if len(highs) < window:
        return {"available": False, "upper": None, "lower": None, "width_pct": None}
    upper = float(highs.max())
    lower = float(lows.min())
    last = float(close.iloc[-1])
    width_pct = (upper - lower) / last * 100 if last else None
    return {"available": True, "upper": upper, "lower": lower, "width_pct": width_pct}


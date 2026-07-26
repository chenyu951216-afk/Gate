import pandas as pd


def breakout_state(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    atr_value: float | None,
    turnover_ratio: float | None,
    adx: float | None,
    period: int = 20,
    atr_multiple: float = 0.25,
) -> dict[str, bool | str]:
    if len(close) < period + 1 or atr_value is None:
        return {"state": "unavailable", "breakout": False, "breakdown": False}
    prior_high = float(high.iloc[-period - 1 : -1].max())
    prior_low = float(low.iloc[-period - 1 : -1].min())
    last = float(close.iloc[-1])
    supported = (turnover_ratio is None or turnover_ratio >= 1.2) and (adx is None or adx >= 18)
    breakout = last > prior_high + atr_multiple * atr_value and supported
    breakdown = last < prior_low - atr_multiple * atr_value and supported
    failed_up = float(close.iloc[-2]) > prior_high and last <= prior_high
    failed_down = float(close.iloc[-2]) < prior_low and last >= prior_low
    state = "breakout" if breakout else "breakdown" if breakdown else "failed_breakout" if failed_up else "failed_breakdown" if failed_down else "range"
    return {"state": state, "breakout": breakout, "breakdown": breakdown, "failed_breakout": failed_up, "failed_breakdown": failed_down}


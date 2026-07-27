import numpy as np
import pandas as pd


def mfi(
    high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series, period: int = 14
) -> pd.Series:
    typical = (high + low + close) / 3
    money_flow = typical * volume
    delta = typical.diff()
    positive = money_flow.where(delta > 0, 0.0).rolling(period, min_periods=period).sum()
    negative = money_flow.where(delta < 0, 0.0).abs().rolling(period, min_periods=period).sum()
    ratio = positive / negative.replace(0, np.nan)
    result = 100 - (100 / (1 + ratio))
    result = result.mask((negative == 0) & (positive > 0), 100.0)
    return result


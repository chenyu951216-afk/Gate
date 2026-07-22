import numpy as np
import pandas as pd


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def dmi_adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> pd.DataFrame:
    high = high.astype(float)
    low = low.astype(float)
    close = close.astype(float)
    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1
    ).max(axis=1)
    atr = _wilder(tr, period)
    plus_di = 100 * _wilder(plus_dm, period) / atr.replace(0, np.nan)
    minus_di = 100 * _wilder(minus_dm, period) / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = _wilder(dx, period)
    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": adx, "atr": atr})


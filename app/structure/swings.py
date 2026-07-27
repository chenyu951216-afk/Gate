import pandas as pd


def causal_swings(high: pd.Series, low: pd.Series, lookback: int = 10) -> pd.DataFrame:
    """Return rolling, past-only extrema; no future candles are used."""
    return pd.DataFrame(
        {
            "rolling_high": high.astype(float).rolling(lookback, min_periods=lookback).max(),
            "rolling_low": low.astype(float).rolling(lookback, min_periods=lookback).min(),
        }
    )


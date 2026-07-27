import pandas as pd


def vwap(high: pd.Series, low: pd.Series, close: pd.Series, volume: pd.Series) -> pd.Series:
    typical = (high.astype(float) + low.astype(float) + close.astype(float)) / 3
    denominator = volume.astype(float).cumsum().replace(0, pd.NA)
    return (typical * volume.astype(float)).cumsum() / denominator


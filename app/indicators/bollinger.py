import pandas as pd


def bollinger(series: pd.Series, period: int = 20, deviations: float = 2.0) -> pd.DataFrame:
    mid = series.astype(float).rolling(period, min_periods=period).mean()
    std = series.astype(float).rolling(period, min_periods=period).std(ddof=0)
    upper = mid + deviations * std
    lower = mid - deviations * std
    bandwidth = (upper - lower) / mid.replace(0, pd.NA)
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "bandwidth": bandwidth})


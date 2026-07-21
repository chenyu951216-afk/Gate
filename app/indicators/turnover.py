import pandas as pd


def turnover_features(turnover: pd.Series) -> pd.DataFrame:
    turnover = turnover.astype(float)
    ma5 = turnover.rolling(5, min_periods=5).mean()
    ma20 = turnover.rolling(20, min_periods=20).mean()
    ratio = turnover / ma20.replace(0, pd.NA)
    return pd.DataFrame(
        {
            "current_turnover": turnover,
            "turnover_ma5": ma5,
            "turnover_ma20": ma20,
            "turnover_ratio": ratio,
            "turnover_ma5_slope": ma5.diff(3) / ma5.shift(3).replace(0, pd.NA),
            "turnover_ma20_slope": ma20.diff(5) / ma20.shift(5).replace(0, pd.NA),
        }
    )


def turnover_state(ratio: float | None) -> str:
    if ratio is None:
        return "unavailable"
    if ratio < 0.8:
        return "contracting"
    if ratio < 1.2:
        return "normal"
    if ratio < 1.5:
        return "expanding"
    if ratio < 2.0:
        return "effective_expansion"
    if ratio < 3.0:
        return "strong_expansion"
    if ratio < 5.0:
        return "climax"
    return "extreme_climax"


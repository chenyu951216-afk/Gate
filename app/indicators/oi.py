import numpy as np
import pandas as pd


def oi_features(oi: pd.Series, bars_per_hour: int = 2) -> pd.DataFrame:
    oi = oi.astype(float)
    change_30m = (oi - oi.shift(1)) / oi.shift(1).replace(0, np.nan) * 100
    change_1h = (oi - oi.shift(bars_per_hour)) / oi.shift(bars_per_hour).replace(0, np.nan) * 100
    change_4h = (oi - oi.shift(bars_per_hour * 4)) / oi.shift(bars_per_hour * 4).replace(0, np.nan) * 100
    slope = oi.diff(3)
    acceleration = slope.diff()
    percentile = oi.rolling(100, min_periods=20).rank(pct=True) * 100
    return pd.DataFrame(
        {
            "current_oi": oi,
            "oi_change_30m_pct": change_30m,
            "oi_change_1h_pct": change_1h,
            "oi_change_4h_pct": change_4h,
            "oi_slope": slope,
            "oi_acceleration": acceleration,
            "oi_percentile": percentile,
        }
    )


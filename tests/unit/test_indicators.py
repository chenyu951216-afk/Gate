import numpy as np
import pandas as pd
from app.indicators.atr import atr
from app.indicators.bollinger import bollinger
from app.indicators.dmi_adx import dmi_adx
from app.indicators.ema import ema
from app.indicators.mfi import mfi
from app.indicators.oi import oi_features
from app.indicators.turnover import turnover_features, turnover_state
from app.indicators.vwap import vwap

def series(size=80):
    close = pd.Series(np.linspace(100, 120, size)); return close + 2, close - 2, close, pd.Series(np.full(size, 1000.0))

def test_ema_and_bollinger_warmup():
    _, _, close, _ = series(); result = ema(close, 20); bands = bollinger(close)
    assert result.iloc[:19].isna().all(); assert pd.isna(bands.iloc[18].mid); assert bands.iloc[-1].upper > bands.iloc[-1].mid > bands.iloc[-1].lower

def test_technical_indicators_are_finite_after_warmup():
    high, low, close, volume = series(); values = [vwap(high, low, close, volume), mfi(high, low, close, volume), atr(high, low, close)]; dmi = dmi_adx(high, low, close); values.extend([dmi.adx, dmi.plus_di, dmi.minus_di])
    assert all(value.dropna().map(np.isfinite).all() for value in values)

def test_turnover_and_oi():
    features = turnover_features(pd.Series([100.0] * 20 + [200.0] * 5)); assert features.iloc[-1].turnover_ratio > 1; assert turnover_state(5) == "extreme_climax"
    oi = oi_features(pd.Series(np.arange(1, 101, dtype=float))); assert oi.iloc[-1].oi_change_30m_pct > 0


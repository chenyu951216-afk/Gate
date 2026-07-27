import pandas as pd
from app.structure.breakouts import breakout_state
from app.structure.ranges import range_features
from app.structure.regimes import regime_state
from app.structure.special_patterns import leverage_pattern

def test_range_breakout_and_regime():
    close = pd.Series([100.0] * 30 + [110.0]); high = close + 1; low = close - 1; state = breakout_state(high, low, close, 2, 2, 30)
    assert state["breakout"]; assert range_features(high, low, close)["available"]; assert regime_state(110, 105, 100, 30, 50) == "bullish"

def test_leverage_pattern():
    assert leverage_pattern(5, 0.1, 15) == "leverage_build_up"; assert leverage_pattern(5, 2, 30) == "short_squeeze"


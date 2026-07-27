from __future__ import annotations

from math import isfinite
from typing import Any

import numpy as np
import pandas as pd

from app.indicators.atr import atr


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number >= 0 else None


def adaptive_volatility_profile(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    *,
    baseline_bars: int = 144,
    recent_bars: int = 12,
    shock_bars: int = 4,
    expansion_ratio: float = 1.50,
    expansion_min_bars: int = 3,
) -> dict[str, Any]:
    """Estimate a robust 30m volatility regime without averaging away a shift.

    The reference distribution is the preceding ~72 hours (144 x 30m) and
    excludes the current two-hour shock window. Medians and quantiles keep one
    liquidation wick from redefining ordinary noise. A high-volatility regime
    is accepted only when most candles in the current two-hour window expand;
    one exceptional candle is classified separately as an isolated spike.
    """
    frame = pd.DataFrame(
        {
            "high": pd.to_numeric(high, errors="coerce"),
            "low": pd.to_numeric(low, errors="coerce"),
            "close": pd.to_numeric(close, errors="coerce"),
        }
    ).replace([np.inf, -np.inf], np.nan).dropna()
    minimum = max(24, shock_bars + 14)
    if len(frame) < minimum:
        return {
            "available": False,
            "regime": "unavailable",
            "bars_used": len(frame),
        }

    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        (
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ),
        axis=1,
    ).max(axis=1)
    tr_pct = (true_range / previous_close.replace(0, np.nan)).replace(
        [np.inf, -np.inf], np.nan
    ).dropna()
    if len(tr_pct) < minimum - 1:
        return {
            "available": False,
            "regime": "unavailable",
            "bars_used": len(tr_pct),
        }

    shock_bars = max(2, min(int(shock_bars), len(tr_pct) // 3))
    recent_bars = max(shock_bars, min(int(recent_bars), len(tr_pct)))
    history_end = max(1, len(tr_pct) - shock_bars)
    history_start = max(0, history_end - max(24, int(baseline_bars)))
    baseline = tr_pct.iloc[history_start:history_end]
    if len(baseline) < 20:
        baseline = tr_pct.iloc[:history_end]

    baseline_median = float(baseline.median())
    q25 = float(baseline.quantile(0.25))
    q75 = float(baseline.quantile(0.75))
    q90 = float(baseline.quantile(0.90))
    q95 = float(baseline.quantile(0.95))
    recent = tr_pct.iloc[-recent_bars:]
    shock = tr_pct.iloc[-shock_bars:]
    recent_median = float(recent.median())
    shock_median = float(shock.median())
    latest = float(shock.iloc[-1])
    scale = max(baseline_median, 1e-12)
    expansion_ratio_value = shock_median / scale
    elevated_count = int((shock > q75).sum())
    required = max(2, min(int(expansion_min_bars), shock_bars))

    confirmed_expansion = (
        expansion_ratio_value >= max(1.0, float(expansion_ratio))
        and elevated_count >= required
    )
    isolated_spike = (
        not confirmed_expansion
        and latest > max(q95, scale * max(1.0, float(expansion_ratio)))
        and elevated_count <= 1
    )
    compression = (
        not confirmed_expansion
        and not isolated_spike
        and recent_median <= 0.70 * scale
        and latest <= q75
    )

    atr_series = atr(frame["high"], frame["low"], frame["close"])
    atr_value = _finite(atr_series.iloc[-1]) if not atr_series.empty else None
    latest_close = float(frame["close"].iloc[-1])
    atr_pct = (atr_value / latest_close) if atr_value and latest_close > 0 else scale

    if confirmed_expansion:
        regime = "expansion"
        effective_pct = max(
            shock_median,
            recent_median,
            min(atr_pct, max(shock_median * 1.50, q90)),
        )
    elif isolated_spike:
        regime = "isolated_spike"
        pre_spike = recent.iloc[:-1]
        pre_spike_median = float(pre_spike.median()) if not pre_spike.empty else scale
        # The wick still remains part of price structure, but it must not
        # inflate every volatility-based distance as if it were persistent.
        effective_pct = max(scale, q75, pre_spike_median)
    elif compression:
        regime = "compression"
        effective_pct = max(q25, recent_median)
    else:
        regime = "normal"
        effective_pct = max(
            scale,
            recent_median,
            min(atr_pct, max(q90, scale)),
        )

    effective_pct = max(1e-8, float(effective_pct))
    return {
        "available": True,
        "regime": regime,
        "bars_used": len(tr_pct),
        "baseline_bars_used": len(baseline),
        "baseline_median_pct": baseline_median,
        "baseline_q25_pct": q25,
        "baseline_q75_pct": q75,
        "baseline_q90_pct": q90,
        "baseline_q95_pct": q95,
        "recent_6h_median_pct": recent_median,
        "recent_2h_median_pct": shock_median,
        "latest_true_range_pct": latest,
        "atr14_pct": atr_pct,
        "effective_atr_pct": effective_pct,
        "effective_atr": effective_pct * latest_close,
        "expansion_ratio": expansion_ratio_value,
        "elevated_bars_2h": elevated_count,
        "required_elevated_bars": required,
        "confirmed_expansion": confirmed_expansion,
        "isolated_spike": isolated_spike,
        "compression": compression,
    }

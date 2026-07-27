from typing import Any


def adaptive_scan_standards(
    volatility_profile: dict[str, Any] | None,
    *,
    base_ranking_score: float,
    trend_score_relief: float,
) -> dict[str, float | str]:
    """Select causal scan thresholds from the coin's own volatility regime.

    Hard requirements such as liquidity, history integrity and executable
    spread are deliberately excluded: volatility may tune signal sensitivity,
    but it must never waive data or execution safety.
    """
    profile = volatility_profile or {}
    regime = str(profile.get("regime") or "normal")
    if regime == "expansion":
        return {
            "regime": regime,
            "min_turnover_ratio": 1.0,
            "min_adx": 16.0,
            "breakout_atr_multiple": 0.20,
            "minimum_direction_score": 57.0,
            "minimum_ranking_score": max(
                0.0, float(base_ranking_score) - float(trend_score_relief)
            ),
        }
    if regime == "compression":
        return {
            "regime": regime,
            "min_turnover_ratio": 1.35,
            "min_adx": 20.0,
            "breakout_atr_multiple": 0.30,
            "minimum_direction_score": 62.0,
            "minimum_ranking_score": float(base_ranking_score) + 2.0,
        }
    if regime == "isolated_spike":
        return {
            "regime": regime,
            "min_turnover_ratio": 1.60,
            "min_adx": 22.0,
            "breakout_atr_multiple": 0.40,
            "minimum_direction_score": 65.0,
            "minimum_ranking_score": float(base_ranking_score) + 5.0,
        }
    return {
        "regime": regime,
        "min_turnover_ratio": 1.20,
        "min_adx": 18.0,
        "breakout_atr_multiple": 0.25,
        "minimum_direction_score": 60.0,
        "minimum_ranking_score": float(base_ranking_score),
    }

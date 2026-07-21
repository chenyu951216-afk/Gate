from typing import Any


RISK_VALUES = {
    "overextended": 5,
    "extreme_turnover": 3,
    "extreme_oi": 4,
    "funding_crowded": 4,
    "basis_crowded": 3,
    "short_squeeze": 3,
    "long_squeeze": 3,
    "failed_breakout": 5,
    "failed_breakdown": 5,
    "mfi_divergence": 3,
    "turnover_divergence": 3,
    "oi_efficiency_decline": 4,
    "low_liquidity": 10,
    "wide_spread": 8,
    "insufficient_history": 15,
    "incomplete_data": 8,
    "stale_data": 8,
    "late_entry": 4,
    "api_partial_failure": 10,
    "historical_data_gap": 10,
    "indicator_failure": 8,
    "time_alignment_error": 20,
    "unsupported_metric": 4,
}


def risk_flags(features: dict[str, Any], missing: list[str], errors: list[str], spread: float | None, settings: Any) -> list[str]:
    flags = set(missing)
    flags.update(errors)
    ratio = features.get("turnover", {}).get("turnover_ratio")
    if ratio is not None and ratio >= 5:
        flags.add("extreme_turnover")
    oi_change = features.get("oi", {}).get("oi_change_30m_pct")
    if oi_change is not None and abs(oi_change) > 10:
        flags.add("extreme_oi")
    if spread is None or spread > settings.max_spread_pct:
        flags.add("wide_spread")
    if features.get("divergence", {}).get("bearish") or features.get("divergence", {}).get("bullish"):
        flags.add("mfi_divergence")
    pattern = features.get("special_pattern")
    if pattern:
        flags.add(pattern)
    if missing or errors:
        flags.add("incomplete_data")
    return sorted(flags)


def risk_penalty(flags: list[str]) -> float:
    return min(35.0, sum(RISK_VALUES.get(flag, 2) for flag in flags))


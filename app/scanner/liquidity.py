from typing import Any


def spread_pct(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 100 if mid else None


def liquidity_quality(turnover: float | None, spread: float | None, settings: Any) -> tuple[bool, float, list[str]]:
    reasons: list[str] = []
    if turnover is None or turnover < settings.min_24h_turnover_usdt:
        reasons.append("low_liquidity")
    if spread is None or spread > settings.max_spread_pct:
        reasons.append("wide_spread" if spread is not None else "spread_unavailable")
    turnover_score = min(1.0, (turnover or 0) / (settings.min_24h_turnover_usdt * 4))
    spread_score = 0.0 if spread is None else max(0.0, min(1.0, 1 - spread / settings.max_spread_pct))
    quality = (turnover_score + spread_score) / 2
    return not reasons, quality * 100, reasons


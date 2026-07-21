from datetime import datetime, timezone
from typing import Any

from app.constants import MAX_TOP_N
from app.scanner.liquidity import liquidity_quality
from app.scanner.risk import risk_flags, risk_penalty
from app.scanner.scoring import WEIGHTS, score_direction


def rank_analysis(analysis: dict[str, Any], settings: Any, top_n: int = 10) -> dict[str, Any] | None:
    if analysis.get("market_state") == "unavailable":
        return None
    features = analysis["features"]
    ticker = features.get("ticker", {})
    allowed, liquidity_score, liquidity_reasons = liquidity_quality(ticker.get("turnover_usdt"), (ticker.get("ask") - ticker.get("bid")) / ((ticker.get("ask") + ticker.get("bid")) / 2) * 100 if ticker.get("bid") and ticker.get("ask") else None, settings)
    missing = list(analysis.get("missing_data", []))
    errors = list(analysis.get("errors", []))
    flags = risk_flags(features, missing + liquidity_reasons, errors, (ticker.get("ask") - ticker.get("bid")) / ((ticker.get("ask") + ticker.get("bid")) / 2) * 100 if ticker.get("bid") and ticker.get("ask") else None, settings)
    bull, bull_weight, bull_reasons = score_direction({**features, "market_state": analysis["market_state"]}, "long")
    bear, bear_weight, bear_reasons = score_direction({**features, "market_state": analysis["market_state"]}, "short")
    available_weight = max(bull_weight, bear_weight)
    completeness = available_weight / sum(WEIGHTS.values()) * 100
    primary = max(bull, bear)
    edge = abs(bull - bear)
    penalty = risk_penalty(flags)
    score = max(0.0, min(100.0, primary * 0.72 + edge * 0.13 + completeness * 0.10 + liquidity_score * 0.05 - penalty))
    direction = "long" if bull >= bear else "short"
    confidence = max(0.0, min(100.0, completeness * 0.45 + edge * 0.35 + max(0.0, 100 - penalty) * 0.20))
    qualifies = (
        allowed
        and completeness >= settings.min_data_completeness_pct
        and max(bull, bear) >= 60
        and score >= settings.ranking_min_score
        and not ({"time_alignment_error", "api_partial_failure"} & set(flags))
    )
    return {
        "contract": analysis.get("contract", ticker.get("contract", "UNKNOWN")),
        "direction": direction,
        "ranking_score": score,
        "bull_score": bull,
        "bear_score": bear,
        "watch_score": max(0.0, min(100.0, (bull + bear) / 2)),
        "confidence": confidence,
        "data_completeness_pct": completeness,
        "risk_penalty": penalty,
        "direction_edge": edge,
        "market_state": analysis["market_state"],
        "signal_state": analysis.get("signal_state", "unknown"),
        "risk_flags": flags,
        "reasons": bull_reasons if direction == "long" else bear_reasons,
        "missing_data": missing,
        "metrics": features,
        "qualifies": qualifies,
        "timestamp": datetime.now(timezone.utc),
    }


def build_rankings(items: list[dict[str, Any]], top_n: int = 10) -> dict[str, list[dict[str, Any]]]:
    top_n = max(1, min(MAX_TOP_N, top_n))
    qualified = [item for item in items if item.get("qualifies")]
    combined = sorted(qualified, key=lambda item: item["ranking_score"], reverse=True)[:top_n]
    longs = sorted((item for item in qualified if item["bull_score"] >= item["bear_score"]), key=lambda item: item["bull_score"], reverse=True)[:top_n]
    shorts = sorted((item for item in qualified if item["bear_score"] > item["bull_score"]), key=lambda item: item["bear_score"], reverse=True)[:top_n]
    for collection in (combined, longs, shorts):
        for rank, item in enumerate(collection, start=1):
            item["rank"] = rank
    return {"combined": combined, "long": longs, "short": shorts}


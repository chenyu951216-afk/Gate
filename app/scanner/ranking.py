from datetime import datetime, timezone
from typing import Any

from app.constants import FATAL_RISK_FLAGS, MAX_TOP_N
from app.scanner.liquidity import liquidity_quality
from app.scanner.risk import risk_flags, risk_penalty
from app.scanner.scoring import (
    WEIGHTS,
    score_direction,
    tactical_score_direction,
)


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
    standards = features.get("scan_standards", {})
    minimum_direction_score = float(
        standards.get("minimum_direction_score", 60)
    )
    minimum_ranking_score = float(
        standards.get("minimum_ranking_score", settings.ranking_min_score)
    )
    fatal = bool(FATAL_RISK_FLAGS & set(flags))
    qualifies = (
        allowed
        and completeness >= settings.min_data_completeness_pct
        and max(bull, bear) >= minimum_direction_score
        and score >= minimum_ranking_score
        and not fatal
    )
    signal_class = "trend"
    signal_horizon = "4h_30m_trend"
    tactical_score = 0.0
    tactical_reasons: list[str] = []
    if bool(getattr(settings, "tactical_signals_enabled", True)):
        tactical_direction = (
            "long"
            if analysis["market_state"] == "bearish"
            else "short"
            if analysis["market_state"] == "bullish"
            else ""
        )
        if tactical_direction:
            tactical_raw, tactical_hard_valid, tactical_reasons = (
                tactical_score_direction(features, tactical_direction)
            )
            tactical_score = max(0.0, tactical_raw - penalty)
            turnover_ratio = features.get("turnover", {}).get("turnover_ratio")
            adx_value = features.get("30m", {}).get("adx")
            tactical_qualifies = (
                allowed
                and not fatal
                and completeness >= settings.min_data_completeness_pct
                and tactical_hard_valid
                and turnover_ratio is not None
                and float(turnover_ratio)
                >= float(settings.tactical_min_turnover_ratio)
                and adx_value is not None
                and float(adx_value) >= float(settings.tactical_min_adx)
                and tactical_score >= float(settings.tactical_min_score)
            )
            if tactical_qualifies:
                direction = tactical_direction
                score = tactical_score
                qualifies = True
                signal_class = "tactical"
                signal_horizon = "30m_tactical"
                confidence = min(
                    100.0,
                    max(confidence, tactical_score * 0.75 + completeness * 0.25),
                )
    return {
        "contract": analysis.get("contract", ticker.get("contract", "UNKNOWN")),
        "contract_type": analysis.get("contract_type", ""),
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
        "reasons": (
            tactical_reasons
            if signal_class == "tactical"
            else bull_reasons
            if direction == "long"
            else bear_reasons
        ),
        "signal_class": signal_class,
        "signal_horizon": signal_horizon,
        "tactical_score": tactical_score,
        "scan_standards": standards,
        "missing_data": missing,
        "metrics": features,
        "qualifies": qualifies,
        "timestamp": datetime.now(timezone.utc),
    }


def build_rankings(items: list[dict[str, Any]], top_n: int = 10) -> dict[str, list[dict[str, Any]]]:
    top_n = max(1, min(MAX_TOP_N, top_n))
    qualified = [item for item in items if item.get("qualifies")]
    combined = sorted(qualified, key=lambda item: item["ranking_score"], reverse=True)[:top_n]
    longs = sorted(
        (
            item
            for item in qualified
            if (
                item.get("direction")
                or (
                "long"
                if item.get("bull_score", 0) >= item.get("bear_score", 0)
                else "short"
                )
            )
            == "long"
        ),
        key=lambda item: item["ranking_score"],
        reverse=True,
    )[:top_n]
    shorts = sorted(
        (
            item
            for item in qualified
            if (
                item.get("direction")
                or (
                "long"
                if item.get("bull_score", 0) >= item.get("bear_score", 0)
                else "short"
                )
            )
            == "short"
        ),
        key=lambda item: item["ranking_score"],
        reverse=True,
    )[:top_n]
    tactical = sorted(
        (
            item
            for item in qualified
            if item.get("signal_class") == "tactical"
        ),
        key=lambda item: item["ranking_score"],
        reverse=True,
    )[:top_n]
    for collection in (combined, longs, shorts, tactical):
        for rank, item in enumerate(collection, start=1):
            item["rank"] = rank
    return {
        "combined": combined,
        "long": longs,
        "short": shorts,
        "tactical": tactical,
    }

from typing import Any

from app.scanner.analyzer import analyze_market


def build_features(bundle: dict[str, Any], min_30m: int, min_4h: int) -> dict[str, Any]:
    ticker = {
        "contract": bundle.get("info_raw", {}).get("name", ""),
        "highest_bid": None,
        "lowest_ask": None,
        "volume_24h_quote": None,
        "change_percentage": None,
        "mark_price": None,
        "index_price": None,
    }
    collected = {**bundle, "ticker": ticker, "snapshot": {}, "trades": []}
    analysis = analyze_market(collected, min_30m, min_4h)
    analysis["historical_unavailable"] = bundle.get("historical_unavailable", [])
    analysis["missing_data"] = sorted(set(analysis.get("missing_data", []) + bundle.get("historical_unavailable", [])))
    analysis["errors"] = sorted(set(analysis.get("errors", []) + bundle.get("collection_errors", [])))
    return analysis

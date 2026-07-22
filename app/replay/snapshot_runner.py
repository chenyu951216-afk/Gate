from typing import Any

from app.scanner.ranking import rank_analysis


def run_snapshot(analysis: dict[str, Any], settings: Any) -> dict[str, Any] | None:
    item = rank_analysis(analysis, settings, 10)
    if item is None:
        return None
    if settings.replay_require_historical_spread and "spread" in analysis.get("missing_data", []):
        item["qualifies"] = False
        item["risk_flags"] = sorted(set(item.get("risk_flags", []) + ["historical_data_gap", "unsupported_metric"]))
        item["missing_data"] = sorted(set(item.get("missing_data", []) + ["historical_spread"]))
    return item


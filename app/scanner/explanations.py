from typing import Any


def explain(item: dict[str, Any]) -> dict[str, Any]:
    reasons = list(item.get("reasons", []))
    if not reasons:
        reasons = ["available indicators did not create a dominant direction"]
    return {
        "reasons": reasons[:6],
        "risks": item.get("risk_flags", [])[:8],
        "missing_data": item.get("missing_data", []),
    }


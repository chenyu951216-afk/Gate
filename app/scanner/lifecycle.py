from collections import Counter
from typing import Any


def annotate_signal_lifecycle(
    rankings: dict[str, list[dict[str, Any]]],
    history: list[dict[str, Any]],
) -> dict[str, int]:
    """Annotate presentation state without changing order eligibility."""
    previous_items = (
        history[0].get("rankings", {}).get("combined", [])
        if history
        else []
    )
    previous_by_contract = {
        str(item.get("contract", "")).upper(): item
        for item in previous_items
        if isinstance(item, dict)
    }
    older_keys = {
        (
            str(item.get("contract", "")).upper(),
            str(item.get("direction", "")).lower(),
        )
        for scan in history[1:]
        for item in scan.get("rankings", {}).get("combined", [])
        if isinstance(item, dict)
    }
    lifecycle_by_key: dict[tuple[str, str], tuple[str, int]] = {}
    for item in rankings.get("combined", []):
        contract = str(item.get("contract", "")).upper()
        direction = str(item.get("direction", "")).lower()
        key = (contract, direction)
        previous = previous_by_contract.get(contract)
        if previous and str(previous.get("direction", "")).lower() == direction:
            consecutive = 1
            for scan in history:
                scan_items = scan.get("rankings", {}).get("combined", [])
                if any(
                    str(old.get("contract", "")).upper() == contract
                    and str(old.get("direction", "")).lower() == direction
                    for old in scan_items
                    if isinstance(old, dict)
                ):
                    consecutive += 1
                else:
                    break
            state = "PERSISTING"
        elif previous:
            state, consecutive = "DIRECTION_FLIP", 1
        elif key in older_keys:
            state, consecutive = "REQUALIFIED", 1
        else:
            state, consecutive = "NEW", 1
        lifecycle_by_key[key] = (state, consecutive)

    counts: Counter[str] = Counter()
    for bucket in ("combined", "long", "short", "tactical"):
        for item in rankings.get(bucket, []):
            key = (
                str(item.get("contract", "")).upper(),
                str(item.get("direction", "")).lower(),
            )
            state, consecutive = lifecycle_by_key.get(key, ("NEW", 1))
            item["signal_lifecycle"] = state
            item["consecutive_qualified_scans"] = consecutive
            if bucket == "combined":
                counts[state] += 1
    return dict(counts)

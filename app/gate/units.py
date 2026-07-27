from typing import Any


def contract_size_to_base(size: float, quanto_multiplier: float | None) -> float | None:
    if quanto_multiplier is None:
        return None
    return float(size) * float(quanto_multiplier)


def contracts_to_quote(size: float, price: float, quanto_multiplier: float | None) -> float | None:
    base = contract_size_to_base(size, quanto_multiplier)
    return None if base is None else base * float(price)


def turnover_from_candle(raw: dict[str, Any], close: float, quanto_multiplier: float | None) -> float | None:
    """Use Gate's `sum` amount when provided; otherwise derive USDT quote value explicitly."""
    if raw.get("sum") not in (None, ""):
        try:
            return float(raw["sum"])
        except (TypeError, ValueError):
            return None
    if raw.get("v") in (None, ""):
        return None
    try:
        return contracts_to_quote(float(raw["v"]), close, quanto_multiplier)
    except (TypeError, ValueError):
        return None


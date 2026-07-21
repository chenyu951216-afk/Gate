from decimal import Decimal, ROUND_DOWN
from typing import Any


class TradingRiskError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def build_execution_plan(
    ranking: dict[str, Any],
    contract_info: Any,
    settings: Any,
    entry_price: float | None = None,
) -> dict[str, Any]:
    side = str(ranking.get("direction", "")).lower()
    if side not in {"long", "short"}:
        raise TradingRiskError("INVALID_DIRECTION", "ranking direction must be long or short")
    metrics = ranking.get("metrics", {})
    ticker = metrics.get("ticker", {})
    entry = _positive(entry_price) or _positive(ticker.get("mark_price")) or _positive(ticker.get("last"))
    if entry is None:
        raise TradingRiskError("NO_ENTRY_PRICE", "no valid mark or last price")

    frame15 = metrics.get("15m", {})
    frame5 = metrics.get("5m", {})
    frame30 = metrics.get("30m", {})
    atr15 = _positive(frame15.get("atr")) or _positive(frame30.get("atr"))
    atr5 = _positive(frame5.get("atr")) or atr15
    if atr15 is None:
        raise TradingRiskError("NO_ATR", "15m/30m ATR is unavailable")

    state = str(ranking.get("market_state", "normal"))
    buffer_atr = float(settings.stop_loss_buffer_atr)
    if state in {"high_volatility", "extreme"}:
        buffer_atr = max(buffer_atr, 1.1)
    elif state == "low_volatility":
        buffer_atr = min(buffer_atr, 0.8)
    buffer_atr = min(1.3, max(0.6, buffer_atr))

    recent_low = _positive(frame15.get("recent_low")) or _positive(frame30.get("recent_low"))
    recent_high = _positive(frame15.get("recent_high")) or _positive(frame30.get("recent_high"))
    if side == "long":
        structure_stop = recent_low - buffer_atr * atr15 if recent_low else None
        fallback_stop = entry - float(settings.fallback_stop_atr) * atr15
        stop = structure_stop if structure_stop is not None and structure_stop < entry else fallback_stop
    else:
        structure_stop = recent_high + buffer_atr * atr15 if recent_high else None
        fallback_stop = entry + float(settings.fallback_stop_atr) * atr15
        stop = structure_stop if structure_stop is not None and structure_stop > entry else fallback_stop

    risk_distance = abs(entry - stop)
    if risk_distance <= 0:
        raise TradingRiskError("INVALID_STOP", "initial stop is on the wrong side of entry")
    if risk_distance > float(settings.max_initial_stop_atr) * atr15:
        raise TradingRiskError("STOP_TOO_FAR", "structure stop exceeds configured ATR risk distance")

    targets = [entry + risk_distance * multiplier if side == "long" else entry - risk_distance * multiplier for multiplier in (1.0, 2.0, 3.0)]
    rr = [abs(target - entry) / risk_distance for target in targets]
    if min(rr) < float(settings.minimum_order_rr):
        raise TradingRiskError("RR_BELOW_MINIMUM", "the first take-profit does not reach minimum RR")

    return {
        "contract": contract_info.name,
        "side": side,
        "entry_price": entry,
        "initial_stop": stop,
        "current_stop": stop,
        "initial_risk_distance": risk_distance,
        "current_r_multiple": 0.0,
        "stop_quality": "STRUCTURE" if structure_stop is not None and stop == structure_stop else "FALLBACK",
        "take_profits": [
            {"stage": "TP1", "price": targets[0], "percent": float(settings.take_profit_1_pct), "rr": rr[0]},
            {"stage": "TP2", "price": targets[1], "percent": float(settings.take_profit_2_pct), "rr": rr[1]},
            {"stage": "TP3", "price": targets[2], "percent": float(settings.take_profit_3_pct), "rr": rr[2]},
        ],
        "runner_percent": float(settings.runner_pct),
        "completed_stages": [],
        "phase": "INITIAL_RISK",
        "protection_order_ids": {"stop": None, "TP1": None, "TP2": None, "TP3": None},
        "last_stop_update": None,
        "last_take_profit_update": None,
        "atr15": atr15,
        "atr5": atr5,
        "market_state": state,
        "risk_flags": list(ranking.get("risk_flags", [])),
        "ranking_score": ranking.get("ranking_score"),
    }


def max_leverage(contract_info: Any, tiers: list[dict[str, Any]] | None = None) -> float | None:
    candidates: list[float] = []
    for value in (
        getattr(contract_info, "leverage_max", None),
        getattr(contract_info, "raw", {}).get("leverage_max"),
    ):
        number = _positive(value)
        if number is not None:
            candidates.append(number)
    for tier in tiers or []:
        number = _positive(tier.get("leverage_max"))
        if number is not None:
            candidates.append(number)
    return max(candidates) if candidates else None


def max_leverage_for_notional(
    contract_info: Any, tiers: list[dict[str, Any]] | None, notional: float
) -> float | None:
    """Return the highest leverage that can accommodate the target risk tier."""
    usable: list[tuple[float, float]] = []
    for tier in tiers or []:
        risk_limit = _positive(tier.get("risk_limit"))
        leverage = _positive(tier.get("leverage_max"))
        if risk_limit is not None and leverage is not None:
            usable.append((risk_limit, leverage))
    if usable:
        usable.sort(key=lambda item: item[0])
        for risk_limit, leverage in usable:
            if notional <= risk_limit:
                return leverage
        return usable[-1][1]
    return max_leverage(contract_info, tiers)


def notional_for_contract(contract_info: Any, price: float, notional: float) -> tuple[str, float]:
    multiplier = _positive(getattr(contract_info, "quanto_multiplier", None))
    if multiplier is None:
        raise TradingRiskError("NO_CONTRACT_MULTIPLIER", "Gate contract multiplier is unavailable")
    if str(getattr(contract_info, "type", "direct")) != "direct":
        raise TradingRiskError("UNSUPPORTED_CONTRACT_TYPE", "USDT execution requires a direct contract")
    raw_size = Decimal(str(notional)) / (Decimal(str(price)) * Decimal(str(multiplier)))
    enable_decimal = bool(getattr(contract_info, "enable_decimal", False))
    step = Decimal("0.00000001") if enable_decimal else Decimal("1")
    size = (raw_size / step).to_integral_value(rounding=ROUND_DOWN) * step
    minimum = Decimal(str(getattr(contract_info, "order_size_min", None) or 0))
    maximum = Decimal(str(getattr(contract_info, "order_size_max", None) or 0))
    if minimum and size < minimum:
        raise TradingRiskError("ORDER_SIZE_TOO_SMALL", "notional is below Gate minimum order size")
    if maximum and size > maximum:
        size = (maximum / step).to_integral_value(rounding=ROUND_DOWN) * step
    if size <= 0:
        raise TradingRiskError("ORDER_SIZE_ZERO", "calculated order size is zero")
    text = format(size, "f").rstrip("0").rstrip(".") or "0"
    return text, float(size * Decimal(str(price)) * Decimal(str(multiplier)))


def signed_size(side: str, size: str | float | Decimal) -> str:
    value = Decimal(str(size))
    if side == "long":
        return format(abs(value), "f").rstrip("0").rstrip(".") or "0"
    return format(-abs(value), "f").rstrip("0").rstrip(".") or "0"


def partial_close_size(side: str, entry_size: str, percent: float) -> str:
    size = Decimal(str(entry_size)) * Decimal(str(percent))
    return signed_size("short" if side == "long" else "long", size)

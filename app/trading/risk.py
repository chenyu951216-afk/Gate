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


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _coinglass_target_prices(
    metrics: dict[str, Any], side: str, entry: float, risk_distance: float, atr15: float
) -> list[float | None]:
    """Convert confirmed CoinGlass liquidation clusters into conservative TP candidates."""
    coinglass = metrics.get("coinglass", {})
    heatmap = coinglass.get("heatmap", {}) if isinstance(coinglass, dict) else {}
    raw_levels = heatmap.get("strongest_levels") or heatmap.get("levels") or []
    candidates: list[float] = []
    front_run = max(0.1 * atr15, entry * 0.001)
    for raw in raw_levels:
        level = _positive(raw.get("price") if isinstance(raw, dict) else None)
        if level is None:
            continue
        target = level - front_run if side == "long" else level + front_run
        distance = target - entry if side == "long" else entry - target
        rr = distance / risk_distance if risk_distance > 0 else 0.0
        if rr >= 1.0 and rr <= 8.0:
            candidates.append(target)
    candidates = sorted(set(candidates), reverse=side == "short")
    selected: list[float | None] = []
    for required_rr in (1.0, 2.0, 3.0):
        selected.append(
            next(
                (
                    target
                    for target in candidates
                    if ((target - entry) if side == "long" else (entry - target)) / risk_distance >= required_rr
                    and (
                        not selected
                        or selected[-1] is None
                        or ((target > selected[-1]) if side == "long" else (target < selected[-1]))
                    )
                ),
                None,
            )
        )
    return selected


def build_execution_plan(
    ranking: dict[str, Any],
    contract_info: Any,
    settings: Any,
    entry_price: float | None = None,
    risk_notional_usdt: float | None = None,
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
    estimated_stop_loss = None
    max_stop_loss = float(getattr(settings, "max_initial_stop_loss_usdt", 1000.0))
    if risk_notional_usdt is not None:
        risk_notional = _positive(risk_notional_usdt)
        if risk_notional is None:
            raise TradingRiskError("INVALID_NOTIONAL", "position notional is unavailable for stop-loss risk")
        estimated_stop_loss = risk_notional * risk_distance / entry
        if max_stop_loss > 0 and estimated_stop_loss > max_stop_loss + 1e-9:
            raise TradingRiskError(
                "STOP_LOSS_OVER_LIMIT",
                f"initial stop loss {estimated_stop_loss:.2f} USDT exceeds {max_stop_loss:.2f} USDT limit",
            )

    base_targets = [
        entry + risk_distance * multiplier if side == "long" else entry - risk_distance * multiplier
        for multiplier in (1.0, 2.0, 3.0)
    ]
    coinglass_targets = _coinglass_target_prices(metrics, side, entry, risk_distance, atr15)
    targets = [candidate or base for candidate, base in zip(coinglass_targets, base_targets, strict=True)]
    rr = [abs(target - entry) / risk_distance for target in targets]
    if min(rr) < float(settings.minimum_order_rr):
        raise TradingRiskError("RR_BELOW_MINIMUM", "the first take-profit does not reach minimum RR")

    return {
        "contract": contract_info.name,
        "price_tick": getattr(contract_info, "order_price_round", None),
        "side": side,
        "entry_price": entry,
        "initial_stop": stop,
        "current_stop": stop,
        "initial_risk_distance": risk_distance,
        "estimated_stop_loss_usdt": estimated_stop_loss,
        "max_initial_stop_loss_usdt": max_stop_loss,
        "current_r_multiple": 0.0,
        "stop_quality": "STRUCTURE" if structure_stop is not None and stop == structure_stop else "FALLBACK",
        "take_profits": [
            {
                "stage": "TP1",
                "price": targets[0],
                "percent": float(settings.take_profit_1_pct),
                "rr": rr[0],
                "source": "coinglass_heatmap" if coinglass_targets[0] else "R_multiple",
            },
            {
                "stage": "TP2",
                "price": targets[1],
                "percent": float(settings.take_profit_2_pct),
                "rr": rr[1],
                "source": "coinglass_heatmap" if coinglass_targets[1] else "R_multiple",
            },
            {
                "stage": "TP3",
                "price": targets[2],
                "percent": float(settings.take_profit_3_pct),
                "rr": rr[2],
                "source": "coinglass_heatmap" if coinglass_targets[2] else "R_multiple",
            },
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
    text = _decimal_text(size)
    return text, float(size * Decimal(str(price)) * Decimal(str(multiplier)))


def notional_from_size(contract_info: Any, price: float, size: str | float | Decimal) -> float:
    multiplier = _positive(getattr(contract_info, "quanto_multiplier", None))
    if multiplier is None:
        raise TradingRiskError("NO_CONTRACT_MULTIPLIER", "Gate contract multiplier is unavailable")
    return abs(float(size)) * float(price) * multiplier


def signed_size(side: str, size: str | float | Decimal) -> str:
    value = Decimal(str(size))
    if side == "long":
        return _decimal_text(abs(value))
    return _decimal_text(-abs(value))


def partial_close_size(side: str, entry_size: str, percent: float) -> str:
    size = Decimal(str(entry_size)) * Decimal(str(percent))
    return signed_size("short" if side == "long" else "long", size)

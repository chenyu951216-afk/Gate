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


def _liquidity_aware_stop(
    metrics: dict[str, Any],
    side: str,
    entry: float,
    stop: float,
    atr15: float,
    buffer_atr: float,
) -> tuple[float, bool]:
    """Keep the invalidation stop beyond a nearby opposing liquidation pool.

    A pool is only considered when it lies between entry and the maximum
    sensible stop range.  This prevents a remote heatmap print from widening
    risk indefinitely.
    """
    coinglass = metrics.get("coinglass", {})
    heatmap = coinglass.get("heatmap", {}) if isinstance(coinglass, dict) else {}
    levels = heatmap.get("strongest_levels") or heatmap.get("levels") or []
    maximum_distance = 4.5 * atr15
    prices = [
        price
        for raw in levels
        if (price := _positive(raw.get("price") if isinstance(raw, dict) else None)) is not None
        and 0 < ((entry - price) if side == "long" else (price - entry)) <= maximum_distance
    ]
    if not prices:
        return stop, False
    # Use the furthest relevant pool: stopping between two nearby pools merely
    # moves the order into the next sweep zone.
    pool = min(prices) if side == "long" else max(prices)
    buffered = pool - buffer_atr * atr15 if side == "long" else pool + buffer_atr * atr15
    improved = buffered < stop if side == "long" else buffered > stop
    return (buffered, True) if improved else (stop, False)


def dynamic_position_notional(
    *,
    equity: float,
    available_margin: float,
    entry: float,
    stop: float,
    metrics: dict[str, Any],
    side: str,
    stop_quality: str,
    market_state: str,
    risk_flags: list[Any] | None,
    settings: Any,
    open_notional: float = 0.0,
    leverage: float = 1.0,
    mode: str = "live",
) -> dict[str, float | str]:
    """Size from monetary risk, then enforce position, portfolio and margin caps."""
    if equity <= 0 or available_margin <= 0:
        raise TradingRiskError("ACCOUNT_EQUITY_UNAVAILABLE", "positive account equity and available margin are required")
    stop_pct = abs(entry - stop) / entry
    if stop_pct <= 0:
        raise TradingRiskError("INVALID_STOP", "stop distance must be positive before sizing")

    assessment = execution_quality_assessment(
        metrics=metrics,
        side=side,
        entry=entry,
        stop=stop,
        stop_quality=stop_quality,
        settings=settings,
    )
    if float(assessment["score"]) < float(settings.minimum_execution_quality_score):
        raise TradingRiskError(
            "EXECUTION_QUALITY_TOO_LOW",
            f"independent execution quality {assessment['score']:.1f} is below the required threshold",
        )
    base = float(settings.risk_per_trade_pct)
    regime = {
        "low_volatility": 1.05,
        "normal": 1.0,
        "bullish": 1.0,
        "bearish": 1.0,
        "high_volatility": 0.80,
        "extreme": 0.60,
    }.get(str(market_state).lower(), 0.90)
    flag_penalty = max(0.60, 1.0 - 0.08 * len(risk_flags or []))
    risk_pct = base * float(assessment["size_factor"]) * regime * flag_penalty
    risk_pct = min(float(settings.max_risk_per_trade_pct), max(float(settings.min_risk_per_trade_pct), risk_pct))
    risk_budget = equity * risk_pct

    raw_notional = risk_budget / stop_pct
    position_cap = equity * float(settings.max_position_notional_equity_multiple)
    portfolio_cap = max(0.0, equity * float(settings.max_portfolio_notional_equity_multiple) - max(0.0, open_notional))
    margin_cap = available_margin * max(1.0, leverage) * float(settings.available_margin_utilization_pct)
    live_notional = min(raw_notional, position_cap, portfolio_cap, margin_cap)
    minimum_viable = equity * float(settings.minimum_viable_position_equity_multiple)
    if live_notional + 1e-9 < minimum_viable:
        raise TradingRiskError(
            "POSITION_BELOW_VIABLE_SIZE",
            f"normal position {live_notional:.2f} is below the strategy minimum {minimum_viable:.2f}; no ant-sized order",
        )
    if mode == "test":
        live_notional *= float(settings.test_mode_notional_multiplier)
    if live_notional <= 0:
        raise TradingRiskError("PORTFOLIO_RISK_EXHAUSTED", "portfolio or available-margin capacity is exhausted")
    return {
        "notional": live_notional,
        "normal_notional": live_notional / (float(settings.test_mode_notional_multiplier) if mode == "test" else 1.0),
        "risk_budget_usdt": risk_budget * (float(settings.test_mode_notional_multiplier) if mode == "test" else 1.0),
        "risk_pct_equity": risk_pct,
        "stop_pct": stop_pct,
        "binding_cap": min(
            (("risk_budget", raw_notional), ("position", position_cap), ("portfolio", portfolio_cap), ("margin", margin_cap)),
            key=lambda item: item[1],
        )[0],
        "execution_quality_score": float(assessment["score"]),
        "execution_quality_factor": float(assessment["size_factor"]),
    }


def execution_quality_assessment(
    *,
    metrics: dict[str, Any],
    side: str,
    entry: float,
    stop: float,
    stop_quality: str,
    settings: Any,
) -> dict[str, float]:
    """Independent per-coin execution assessment, separate from scan ranking."""
    ticker = metrics.get("ticker", {}) if isinstance(metrics, dict) else {}
    bid = _positive(ticker.get("bid") or ticker.get("highest_bid"))
    ask = _positive(ticker.get("ask") or ticker.get("lowest_ask"))
    turnover = _positive(ticker.get("turnover_usdt") or ticker.get("volume_24h_quote"))
    spread_pct = ((ask - bid) / ((ask + bid) / 2) * 100) if bid and ask and ask >= bid else None

    atr = _positive(metrics.get("15m", {}).get("atr")) or _positive(metrics.get("30m", {}).get("atr"))
    atr_pct = (atr / entry * 100) if atr else None
    liquidity = 50.0
    if turnover is not None:
        liquidity = min(100.0, 35.0 + 65.0 * turnover / max(1.0, float(settings.min_24h_turnover_usdt) * 4))
    if spread_pct is not None:
        spread_score = max(0.0, 100.0 * (1.0 - spread_pct / max(0.0001, float(settings.max_spread_pct))))
        liquidity = (liquidity + spread_score) / 2

    volatility = 60.0
    if atr_pct is not None:
        # Efficient crypto volatility is tradable; both dead markets and
        # disorderly expansion receive less capital.
        volatility = 100.0 if 0.35 <= atr_pct <= 2.5 else 70.0 if 0.20 <= atr_pct <= 4.0 else 40.0
    structure = {"STRUCTURE": 90.0, "LIQUIDITY_ADJUSTED": 85.0, "FALLBACK": 60.0}.get(stop_quality, 55.0)
    stop_atr = abs(entry - stop) / atr if atr else 2.5
    if stop_atr > 4.0:
        structure -= 15.0

    crowding = 70.0
    funding_value = _positive(metrics.get("funding_rate"))
    if funding_value is None:
        try:
            funding_value = abs(float(metrics.get("funding_rate")))
        except (TypeError, ValueError):
            funding_value = None
    funding = funding_value
    if funding is not None:
        crowding -= min(30.0, funding * 100_000)
    coinglass = metrics.get("coinglass", {})
    liquidation = coinglass.get("liquidation", {}) if isinstance(coinglass, dict) else {}
    bias = float(liquidation.get("directional_bias") or 0.0)
    favorable_bias = bias if side == "long" else -bias
    crowding += max(-15.0, min(15.0, favorable_bias * 25.0))
    crowding = max(20.0, min(100.0, crowding))

    score = 0.35 * liquidity + 0.25 * volatility + 0.25 * structure + 0.15 * crowding
    factor = 0.75 + max(0.0, min(1.0, (score - 45.0) / 45.0)) * 0.50
    return {
        "score": score,
        "size_factor": factor,
        "liquidity_score": liquidity,
        "volatility_score": volatility,
        "structure_score": structure,
        "crowding_score": crowding,
    }


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

    # A stale/outlier swing must not force an unusably wide stop. In that case
    # use the volatility invalidation; dynamic sizing will still reduce size.
    maximum_distance = float(getattr(settings, "maximum_stop_atr", 4.5)) * atr15
    if abs(entry - stop) > maximum_distance:
        stop = fallback_stop
        structure_stop = None
    minimum_distance = float(getattr(settings, "minimum_stop_atr", 1.6)) * atr15
    if abs(entry - stop) < minimum_distance:
        stop = entry - minimum_distance if side == "long" else entry + minimum_distance
    stop, liquidity_adjusted = _liquidity_aware_stop(
        metrics,
        side,
        entry,
        stop,
        atr15,
        float(getattr(settings, "liquidity_stop_buffer_atr", 0.35)),
    )

    risk_distance = abs(entry - stop)
    if risk_distance <= 0:
        raise TradingRiskError("INVALID_STOP", "initial stop is on the wrong side of entry")
    if risk_distance > maximum_distance + 1e-9:
        raise TradingRiskError("STOP_TOO_WIDE", "structure/liquidity invalidation is beyond the maximum tradable stop range")
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

    base_multipliers = (minimum_rr := float(settings.minimum_order_rr), max(2.0, minimum_rr + 0.8), max(3.0, minimum_rr + 1.8))
    base_targets = [
        entry + risk_distance * multiplier if side == "long" else entry - risk_distance * multiplier
        for multiplier in base_multipliers
    ]
    coinglass_targets = _coinglass_target_prices(metrics, side, entry, risk_distance, atr15)
    # CoinGlass is a supplemental target source, not a reason to violate the
    # system RR contract.  Re-validate each selected level here because the
    # entry can be refreshed from Bitget after ranking and because malformed
    # cached heatmap data must never block a valid R-multiple fallback.
    targets: list[float] = []
    sources: list[str] = []
    for candidate, base in zip(coinglass_targets, base_targets, strict=True):
        candidate_distance = (
            candidate - entry if side == "long" else entry - candidate
        ) if candidate is not None else 0.0
        candidate_rr = candidate_distance / risk_distance if risk_distance > 0 else 0.0
        valid_candidate = (
            candidate is not None
            and candidate_distance > 0
            and candidate_rr + 1e-9 >= minimum_rr
        )
        if valid_candidate:
            assert candidate is not None
            targets.append(float(candidate))
        else:
            targets.append(float(base))
        sources.append("coinglass_heatmap" if valid_candidate else "R_multiple")
    rr = [abs(target - entry) / risk_distance for target in targets]
    if min(rr) + 1e-9 < minimum_rr:
        raise TradingRiskError("RR_BELOW_MINIMUM", "the first take-profit does not reach minimum RR")

    return {
        "contract": contract_info.name,
        "enable_decimal": bool(getattr(contract_info, "enable_decimal", False)),
        "price_tick": getattr(contract_info, "order_price_round", None),
        "side": side,
        "entry_price": entry,
        "initial_stop": stop,
        "current_stop": stop,
        "initial_risk_distance": risk_distance,
        "estimated_stop_loss_usdt": estimated_stop_loss,
        "max_initial_stop_loss_usdt": max_stop_loss,
        "current_r_multiple": 0.0,
        "stop_quality": "LIQUIDITY_ADJUSTED" if liquidity_adjusted else "STRUCTURE" if structure_stop is not None and stop == structure_stop else "FALLBACK",
        "take_profits": [
            {
                "stage": "TP1",
                "price": targets[0],
                "percent": float(settings.take_profit_1_pct),
                "rr": rr[0],
                "source": sources[0],
            },
            {
                "stage": "TP2",
                "price": targets[1],
                "percent": float(settings.take_profit_2_pct),
                "rr": rr[1],
                "source": sources[1],
            },
            {
                "stage": "TP3",
                "price": targets[2],
                "percent": float(settings.take_profit_3_pct),
                "rr": rr[2],
                "source": sources[2],
            },
        ],
        "runner_percent": float(settings.runner_pct),
        "completed_stages": [],
        "phase": "INITIAL_RISK",
        "protection_order_ids": {"stop": None, "TP1": None, "TP2": None, "TP3": None},
        "last_stop_update": None,
        "last_take_profit_update": None,
        "last_scan_tp_update": None,
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
        raise TradingRiskError("NO_CONTRACT_MULTIPLIER", "execution contract multiplier is unavailable")
    if str(getattr(contract_info, "type", "direct")) != "direct":
        raise TradingRiskError("UNSUPPORTED_CONTRACT_TYPE", "USDT execution requires a direct contract")
    raw_size = Decimal(str(notional)) / (Decimal(str(price)) * Decimal(str(multiplier)))
    enable_decimal = bool(getattr(contract_info, "enable_decimal", False))
    # Bitget publishes an explicit quantity multiplier (sizeMultiplier),
    # whereas Gate's decimal contracts use a fixed decimal precision.  Using
    # the exchange-provided step prevents otherwise valid entries/TPs from
    # being rejected as an invalid quantity.
    raw_contract = getattr(contract_info, "raw", {}) or {}
    raw_step = raw_contract.get("sizeMultiplier") or raw_contract.get("size_multiplier")
    step = Decimal(str(raw_step)) if enable_decimal and raw_step not in (None, "", "0") else Decimal("0.00000001") if enable_decimal else Decimal("1")
    size = (raw_size / step).to_integral_value(rounding=ROUND_DOWN) * step
    minimum = Decimal(str(getattr(contract_info, "order_size_min", None) or 0))
    maximum = Decimal(str(getattr(contract_info, "order_size_max", None) or 0))
    if minimum and size < minimum:
        raise TradingRiskError("ORDER_SIZE_TOO_SMALL", "notional is below Bitget minimum order size")
    if maximum and size > maximum:
        size = (maximum / step).to_integral_value(rounding=ROUND_DOWN) * step
    if size <= 0:
        raise TradingRiskError("ORDER_SIZE_ZERO", "calculated order size is zero")
    text = _decimal_text(size)
    return text, float(size * Decimal(str(price)) * Decimal(str(multiplier)))


def notional_from_size(contract_info: Any, price: float, size: str | float | Decimal) -> float:
    multiplier = _positive(getattr(contract_info, "quanto_multiplier", None))
    if multiplier is None:
        raise TradingRiskError("NO_CONTRACT_MULTIPLIER", "execution contract multiplier is unavailable")
    return abs(float(size)) * float(price) * multiplier


def signed_size(side: str, size: str | float | Decimal) -> str:
    value = Decimal(str(size))
    if side == "long":
        return _decimal_text(abs(value))
    return _decimal_text(-abs(value))


def partial_close_size(side: str, entry_size: str, percent: float) -> str:
    size = Decimal(str(entry_size)) * Decimal(str(percent))
    return signed_size("short" if side == "long" else "long", size)

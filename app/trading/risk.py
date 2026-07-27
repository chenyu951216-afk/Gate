from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from math import isfinite
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
    return number if number > 0 and isfinite(number) else None


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _target_candidates(
    metrics: dict[str, Any],
    side: str,
    entry: float,
    risk_distance: float,
    atr15: float,
) -> list[dict[str, float | str]]:
    """Build causal profit references from structure, range and liquidation data.

    The scanner's 30m/4h price structure is the primary map. CoinGlass levels
    are supplemental destinations and are deliberately front-run because a
    heatmap represents an estimated concentration rather than an executable
    price guarantee.
    """
    candidates: list[dict[str, float | str]] = []
    structure_front_run = max(0.12 * atr15, entry * 0.0008)
    for frame_name in ("15m", "30m", "4h"):
        frame = metrics.get(frame_name, {})
        if not isinstance(frame, dict):
            continue
        raw_level = frame.get("recent_high" if side == "long" else "recent_low")
        level = _positive(raw_level)
        if level is None:
            continue
        target = level - structure_front_run if side == "long" else level + structure_front_run
        distance = target - entry if side == "long" else entry - target
        if distance > 0:
            candidates.append(
                {
                    "price": target,
                    "rr": distance / risk_distance,
                    "source": f"{frame_name}_structure",
                }
            )

    range_data = metrics.get("range", {})
    if isinstance(range_data, dict):
        level = _positive(range_data.get("upper" if side == "long" else "lower"))
        if level is not None:
            target = level - structure_front_run if side == "long" else level + structure_front_run
            distance = target - entry if side == "long" else entry - target
            if distance > 0:
                candidates.append(
                    {
                        "price": target,
                        "rr": distance / risk_distance,
                        "source": "30m_range_boundary",
                    }
                )

    coinglass = metrics.get("coinglass", {})
    heatmap = coinglass.get("heatmap", {}) if isinstance(coinglass, dict) else {}
    raw_levels = heatmap.get("strongest_levels") or heatmap.get("levels") or []
    liquidity_front_run = max(0.18 * atr15, entry * 0.001)
    for raw in raw_levels:
        level = _positive(raw.get("price") if isinstance(raw, dict) else None)
        if level is None:
            continue
        target = level - liquidity_front_run if side == "long" else level + liquidity_front_run
        distance = target - entry if side == "long" else entry - target
        rr = distance / risk_distance if risk_distance > 0 else 0.0
        if 0 < rr <= 8.0:
            candidates.append(
                {
                    "price": target,
                    "rr": rr,
                    "source": "coinglass_heatmap",
                }
            )
    return sorted(
        candidates,
        key=lambda item: float(item["price"]),
        reverse=side == "short",
    )


def _volatility_profile(metrics: dict[str, Any]) -> dict[str, Any]:
    profile = metrics.get("volatility_72h", {}) if isinstance(metrics, dict) else {}
    return profile if isinstance(profile, dict) and profile.get("available") else {}


def _planning_atr(
    *,
    metrics: dict[str, Any],
    entry: float,
    atr15: float,
) -> tuple[float, dict[str, Any]]:
    """Translate the robust 30m regime estimate into one shared risk scale."""
    profile = _volatility_profile(metrics)
    effective_pct = _positive(profile.get("effective_atr_pct"))
    if effective_pct is None:
        return atr15, profile
    robust_atr = entry * effective_pct
    regime = str(profile.get("regime", "normal"))
    if regime == "isolated_spike":
        # Do not let one wick widen every order, while price structure still
        # records the wick and can invalidate a setup independently.
        planning = robust_atr
    elif regime == "compression":
        # Decay slowly into a quiet state; a brief dead-liquidity pocket must
        # not instantly collapse the stop distance.
        planning = max(robust_atr, min(atr15, robust_atr * 1.25))
    else:
        planning = max(atr15, robust_atr)
    return max(planning, entry * 1e-8), profile


def _liquidity_aware_stop(
    metrics: dict[str, Any],
    side: str,
    entry: float,
    stop: float,
    atr15: float,
    buffer_atr: float,
    confluence_atr: float,
    maximum_distance: float,
) -> tuple[float, bool]:
    """Keep a structural invalidation beyond an overlapping liquidation pool.

    CoinGlass is never allowed to invent a stop on its own. A pool must overlap
    the already selected 15m/30m invalidation zone; remote heatmap prints are
    ignored even when they remain inside the absolute maximum stop envelope.
    """
    coinglass = metrics.get("coinglass", {})
    heatmap = coinglass.get("heatmap", {}) if isinstance(coinglass, dict) else {}
    levels = heatmap.get("strongest_levels") or heatmap.get("levels") or []
    prices = [
        price
        for raw in levels
        if (price := _positive(raw.get("price") if isinstance(raw, dict) else None)) is not None
        and 0 < ((entry - price) if side == "long" else (price - entry))
        and abs(price - stop) <= confluence_atr * atr15
    ]
    if not prices:
        return stop, False
    pool = min(prices) if side == "long" else max(prices)
    buffered = pool - buffer_atr * atr15 if side == "long" else pool + buffer_atr * atr15
    improved = buffered < stop if side == "long" else buffered > stop
    within_envelope = abs(entry - buffered) <= maximum_distance + 1e-9
    return (buffered, True) if improved and within_envelope else (stop, False)


def _multi_timeframe_invalidation(
    *,
    metrics: dict[str, Any],
    side: str,
    entry: float,
    atr15: float,
    atr30: float,
    buffer_atr: float,
    settings: Any,
) -> tuple[float, str, dict[str, float | None]]:
    """Select the invalidation that matches the scanner's timeframe contract.

    The 30m swing invalidates the scan thesis and therefore has priority when
    it remains tradable. The 15m swing is the tactical fallback. The 4h frame
    describes regime and target space, but is intentionally not used as the
    exact stop because a 4h extreme would usually create an unusable risk
    distance for this strategy.
    """
    frame15 = metrics.get("15m", {}) if isinstance(metrics.get("15m", {}), dict) else {}
    frame30 = metrics.get("30m", {}) if isinstance(metrics.get("30m", {}), dict) else {}
    maximum_distance = max(
        float(getattr(settings, "maximum_stop_atr", 4.5)) * atr15,
        entry * float(getattr(settings, "minimum_stop_pct", 0.01)),
    )
    minimum_distance = max(
        float(getattr(settings, "minimum_stop_atr", 1.6)) * atr15,
        entry * float(getattr(settings, "minimum_stop_pct", 0.01)),
    )
    tactical_level = _positive(frame15.get("recent_low" if side == "long" else "recent_high"))
    thesis_level = _positive(frame30.get("recent_low" if side == "long" else "recent_high"))
    tactical_stop = None
    thesis_stop = None
    if tactical_level is not None:
        tactical_stop = (
            tactical_level - buffer_atr * atr15
            if side == "long"
            else tactical_level + buffer_atr * atr15
        )
    if thesis_level is not None:
        thesis_buffer = max(
            float(getattr(settings, "thesis_stop_buffer_atr", 0.55)) * atr30,
            0.35 * atr15,
        )
        thesis_stop = (
            thesis_level - thesis_buffer
            if side == "long"
            else thesis_level + thesis_buffer
        )

    def valid(candidate: float | None) -> bool:
        if candidate is None:
            return False
        directional = candidate < entry if side == "long" else candidate > entry
        return directional and abs(entry - candidate) <= maximum_distance + 1e-9

    thesis_valid = thesis_stop is not None and valid(thesis_stop)
    tactical_valid = tactical_stop is not None and valid(tactical_stop)
    if thesis_valid and tactical_valid:
        assert thesis_stop is not None and tactical_stop is not None
        thesis_is_wider = (
            thesis_stop <= tactical_stop
            if side == "long"
            else thesis_stop >= tactical_stop
        )
        stop = thesis_stop if thesis_is_wider else tactical_stop
        source = "30m_THESIS" if thesis_is_wider else "15m_STRUCTURE_SUPERSEDES_TIGHTER_30m"
    elif thesis_valid:
        assert thesis_stop is not None
        stop = thesis_stop
        source = "30m_THESIS"
    elif tactical_valid:
        assert tactical_stop is not None
        stop = tactical_stop
        source = "15m_STRUCTURE"
    else:
        stop = (
            entry - float(settings.fallback_stop_atr) * atr15
            if side == "long"
            else entry + float(settings.fallback_stop_atr) * atr15
        )
        source = "VOLATILITY_FALLBACK"

    if abs(entry - stop) < minimum_distance:
        stop = entry - minimum_distance if side == "long" else entry + minimum_distance
        source = f"{source}_NOISE_FLOOR"
    return stop, source, {
        "15m_structure_stop": tactical_stop,
        "30m_thesis_stop": thesis_stop,
        "minimum_distance": minimum_distance,
        "maximum_distance": maximum_distance,
    }


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
    base = float(settings.risk_per_trade_pct)
    regime = {
        "low_volatility": 1.05,
        "normal": 1.0,
        "bullish": 1.0,
        "bearish": 1.0,
        "high_volatility": 0.80,
        "extreme": 0.60,
    }.get(str(market_state).lower(), 0.90)
    volatility_regime = str(_volatility_profile(metrics).get("regime", "normal"))
    volatility_risk = {
        "expansion": 0.90,
        "isolated_spike": 0.85,
        "compression": 0.92,
        "normal": 1.0,
    }.get(volatility_regime, 1.0)
    flag_penalty = max(0.60, 1.0 - 0.08 * len(risk_flags or []))
    risk_pct = (
        base
        * float(assessment["size_factor"])
        * regime
        * volatility_risk
        * flag_penalty
    )
    risk_pct = min(float(settings.max_risk_per_trade_pct), max(float(settings.min_risk_per_trade_pct), risk_pct))
    risk_budget = equity * risk_pct

    raw_notional = risk_budget / stop_pct
    position_cap = equity * float(settings.max_position_notional_equity_multiple)
    portfolio_cap = max(0.0, equity * float(settings.max_portfolio_notional_equity_multiple) - max(0.0, open_notional))
    margin_cap = available_margin * max(1.0, leverage) * float(settings.available_margin_utilization_pct)
    minimum_viable = equity * float(settings.minimum_viable_position_equity_multiple)
    promoted_risk_cap = equity * float(settings.max_promoted_risk_per_trade_pct) / stop_pct
    desired_notional = max(raw_notional, min(minimum_viable, promoted_risk_cap))
    live_notional = min(desired_notional, position_cap, portfolio_cap, margin_cap)
    if mode == "test":
        live_notional *= float(settings.test_mode_notional_multiplier)
    if live_notional <= 0:
        raise TradingRiskError("PORTFOLIO_RISK_EXHAUSTED", "portfolio or available-margin capacity is exhausted")
    return {
        "notional": live_notional,
        "normal_notional": live_notional / (float(settings.test_mode_notional_multiplier) if mode == "test" else 1.0),
        "risk_budget_usdt": risk_budget * (float(settings.test_mode_notional_multiplier) if mode == "test" else 1.0),
        "risk_pct_equity": risk_pct,
        "actual_risk_pct_equity": live_notional * stop_pct / equity,
        "promoted_from_risk_size": live_notional > raw_notional + 1e-9,
        "minimum_viable_notional": minimum_viable,
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

    profile = _volatility_profile(metrics)
    profile_pct = _positive(profile.get("effective_atr_pct"))
    atr = (
        entry * profile_pct
        if profile_pct is not None
        else _positive(metrics.get("15m", {}).get("atr"))
        or _positive(metrics.get("30m", {}).get("atr"))
    )
    atr_pct = (atr / entry * 100) if atr else None
    liquidity = 50.0
    if turnover is not None:
        liquidity = min(100.0, 35.0 + 65.0 * turnover / max(1.0, float(settings.min_24h_turnover_usdt) * 4))
    if spread_pct is not None:
        spread_score = max(0.0, 100.0 * (1.0 - spread_pct / max(0.0001, float(settings.max_spread_pct))))
        liquidity = (liquidity + spread_score) / 2
    order_book = metrics.get("order_book", {}) if isinstance(metrics, dict) else {}
    book_side = order_book.get("asks" if side == "long" else "bids", []) if isinstance(order_book, dict) else []
    depth_range = float(getattr(settings, "order_book_depth_range_pct", 0.005))
    depth_notional = sum(
        _positive(level.get("notional")) or 0.0
        for level in book_side
        if isinstance(level, dict)
        and abs((_positive(level.get("price")) or entry) - entry) / entry <= depth_range
    )
    if depth_notional > 0:
        depth_score = min(100.0, 35.0 + 65.0 * depth_notional / max(1.0, float(settings.min_24h_turnover_usdt) * 0.002))
        liquidity = 0.65 * liquidity + 0.35 * depth_score

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
            funding_value = abs(float(str(metrics.get("funding_rate"))))
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
        "depth_notional": depth_notional,
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
    atr30 = _positive(frame30.get("atr")) or atr15
    atr5 = _positive(frame5.get("atr")) or atr15
    if atr15 is None:
        raise TradingRiskError("NO_ATR", "15m/30m ATR is unavailable")
    assert atr30 is not None
    planning_atr, volatility_profile = _planning_atr(
        metrics=metrics,
        entry=entry,
        atr15=atr15,
    )

    state = str(ranking.get("market_state", "normal"))
    buffer_atr = float(settings.stop_loss_buffer_atr)
    if state in {"high_volatility", "extreme"}:
        buffer_atr = max(buffer_atr, 1.1)
    elif state == "low_volatility":
        buffer_atr = min(buffer_atr, 0.8)
    buffer_atr = min(1.3, max(0.6, buffer_atr))

    stop, stop_source, stop_components = _multi_timeframe_invalidation(
        metrics=metrics,
        side=side,
        entry=entry,
        atr15=planning_atr,
        atr30=atr30,
        buffer_atr=buffer_atr,
        settings=settings,
    )
    maximum_distance = float(stop_components["maximum_distance"] or 0.0)
    stop, liquidity_adjusted = _liquidity_aware_stop(
        metrics,
        side,
        entry,
        stop,
        planning_atr,
        float(getattr(settings, "liquidity_stop_buffer_atr", 0.35)),
        float(getattr(settings, "coinglass_stop_confluence_atr", 0.75)),
        maximum_distance,
    )
    if liquidity_adjusted:
        stop_source = f"{stop_source}+COINGLASS_CONFLUENCE"

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

    minimum_rr = float(settings.minimum_order_rr)
    fee_distance = entry * float(getattr(settings, "estimated_round_trip_fee_pct", 0.0012))
    net_rr = float(getattr(settings, "minimum_net_rr", 1.0))
    first_distance = max(minimum_rr * risk_distance, net_rr * risk_distance + fee_distance)
    first_multiple = first_distance / risk_distance
    volatility_regime = str(volatility_profile.get("regime", "normal"))
    aligned_trend = (side == "long" and state == "bullish") or (
        side == "short" and state == "bearish"
    )
    if volatility_regime == "expansion" and aligned_trend:
        stage_two, stage_three = 2.40, 3.80
        tp_percents = (0.25, 0.30, 0.25)
        runner_percent = 0.20
        allocation_source = "confirmed_expansion_trend"
    elif volatility_regime in {"compression", "isolated_spike"} or state in {
        "range",
        "low_volatility",
        "mixed",
    }:
        stage_two, stage_three = 1.80, 2.60
        tp_percents = (0.35, 0.30, 0.20)
        runner_percent = 0.15
        allocation_source = f"{volatility_regime or state}_defensive"
    else:
        stage_two, stage_three = 2.00, 3.00
        tp_percents = (
            float(settings.take_profit_1_pct),
            float(settings.take_profit_2_pct),
            float(settings.take_profit_3_pct),
        )
        runner_percent = float(settings.runner_pct)
        allocation_source = "configured_normal"
    base_multipliers = (
        first_multiple,
        max(stage_two, first_multiple + 0.8),
        max(stage_three, first_multiple + 1.8),
    )
    base_targets = [
        entry + risk_distance * multiplier if side == "long" else entry - risk_distance * multiplier
        for multiplier in base_multipliers
    ]
    candidates = _target_candidates(metrics, side, entry, risk_distance, planning_atr)
    targets: list[float] = []
    sources: list[str] = []
    previous_rr = 0.0
    for index, (_base, base_multiple) in enumerate(
        zip(base_targets, base_multipliers, strict=True)
    ):
        required_rr = max(base_multiple, previous_rr + (0.0 if index == 0 else 0.50))
        maximum_stage_rr = (2.25, 4.25, 8.0)[index]
        selected = next(
            (
                candidate
                for candidate in candidates
                if float(candidate["rr"]) + 1e-9 >= required_rr
                and float(candidate["rr"]) <= maximum_stage_rr + 1e-9
                and (
                    not targets
                    or (
                        float(candidate["price"]) > targets[-1]
                        if side == "long"
                        else float(candidate["price"]) < targets[-1]
                    )
                )
            ),
            None,
        )
        if selected is None:
            target_rr = required_rr
            target = (
                entry + target_rr * risk_distance
                if side == "long"
                else entry - target_rr * risk_distance
            )
            source = "R_multiple"
        else:
            target = float(selected["price"])
            target_rr = float(selected["rr"])
            source = str(selected["source"])
        candidate_distance = target - entry if side == "long" else entry - target
        candidate_net_rr = (candidate_distance - fee_distance) / risk_distance
        if candidate_net_rr + 1e-9 < net_rr:
            target_rr = max(target_rr, net_rr + fee_distance / risk_distance)
            target = (
                entry + target_rr * risk_distance
                if side == "long"
                else entry - target_rr * risk_distance
            )
            source = "fee_adjusted_R_multiple"
        targets.append(float(target))
        sources.append(source)
        previous_rr = abs(target - entry) / risk_distance
    rr = [abs(target - entry) / risk_distance for target in targets]
    if min(rr) + 1e-9 < minimum_rr:
        raise TradingRiskError("RR_BELOW_MINIMUM", "the first take-profit does not reach minimum RR")

    return {
        "contract": contract_info.name,
        "enable_decimal": bool(getattr(contract_info, "enable_decimal", False)),
        "price_tick": getattr(contract_info, "order_price_round", None),
        "size_step": (
            (getattr(contract_info, "raw", {}) or {}).get("sizeMultiplier")
            or (0.00000001 if bool(getattr(contract_info, "enable_decimal", False)) else 1)
        ),
        "order_size_min": getattr(contract_info, "order_size_min", None),
        "side": side,
        "entry_price": entry,
        "initial_stop": stop,
        "current_stop": stop,
        "initial_risk_distance": risk_distance,
        "estimated_stop_loss_usdt": estimated_stop_loss,
        "max_initial_stop_loss_usdt": max_stop_loss,
        "current_r_multiple": 0.0,
        "stop_quality": "LIQUIDITY_ADJUSTED" if liquidity_adjusted else "FALLBACK" if stop_source.startswith("VOLATILITY") else "STRUCTURE",
        "stop_source": stop_source,
        "stop_components": stop_components,
        "take_profits": [
            {
                "stage": "TP1",
                "price": targets[0],
                "percent": tp_percents[0],
                "rr": rr[0],
                "net_rr": (abs(targets[0] - entry) - fee_distance) / risk_distance,
                "source": sources[0],
            },
            {
                "stage": "TP2",
                "price": targets[1],
                "percent": tp_percents[1],
                "rr": rr[1],
                "net_rr": (abs(targets[1] - entry) - fee_distance) / risk_distance,
                "source": sources[1],
            },
            {
                "stage": "TP3",
                "price": targets[2],
                "percent": tp_percents[2],
                "rr": rr[2],
                "net_rr": (abs(targets[2] - entry) - fee_distance) / risk_distance,
                "source": sources[2],
            },
        ],
        "runner_percent": runner_percent,
        "take_profit_allocation_source": allocation_source,
        "completed_stages": [],
        "phase": "INITIAL_RISK",
        "favorable_extreme": entry,
        "peak_r_multiple": 0.0,
        "trail_source": "initial_invalidation",
        "protection_order_ids": {"stop": None, "TP1": None, "TP2": None, "TP3": None},
        "last_stop_update": None,
        "last_take_profit_update": None,
        "atr15": atr15,
        "atr30": atr30,
        "atr5": atr5,
        "planning_atr": planning_atr,
        "volatility_profile": volatility_profile,
        "market_state": state,
        "risk_flags": list(ranking.get("risk_flags", [])),
        "ranking_score": ranking.get("ranking_score"),
    }


def managed_stop_candidate(
    *,
    plan: dict[str, Any],
    context: dict[str, Any],
    price: float,
    entry: float,
    settings: Any,
) -> dict[str, float | str]:
    """Advance one monotonic, regime-aware trailing-stop state machine.

    The initial 30m/15m invalidation remains untouched while the trade is
    proving itself. After sufficient favorable excursion, the stop progresses
    through fee-covered break-even, 15m structure and a volatility chandelier.
    A 5m candle may confirm weakness elsewhere, but never defines the initial
    risk or a premature trailing distance.
    """
    side = str(plan.get("side", "")).lower()
    risk_distance = _positive(plan.get("initial_risk_distance")) or 0.0
    atr15 = _positive(context.get("atr15")) or _positive(plan.get("atr15")) or 0.0
    context_profile = context.get("volatility_72h", {})
    profile = (
        context_profile
        if isinstance(context_profile, dict) and context_profile.get("available")
        else plan.get("volatility_profile", {})
    )
    profile = profile if isinstance(profile, dict) else {}
    volatility_regime = str(profile.get("regime", "normal"))
    robust_atr = _positive(profile.get("effective_atr"))
    if robust_atr is None:
        effective_pct = _positive(profile.get("effective_atr_pct"))
        robust_atr = price * effective_pct if effective_pct is not None else None
    if robust_atr is not None:
        if volatility_regime in {"isolated_spike", "compression"}:
            management_atr = robust_atr
        else:
            management_atr = max(atr15, robust_atr)
    else:
        management_atr = atr15
    current_stop = _positive(plan.get("current_stop")) or 0.0
    if side not in {"long", "short"} or price <= 0 or entry <= 0 or risk_distance <= 0 or management_atr <= 0:
        return {
            "candidate_stop": current_stop,
            "phase": str(plan.get("phase", "INITIAL_RISK")),
            "favorable_extreme": price,
            "peak_r_multiple": 0.0,
            "trail_source": "unchanged_invalid_context",
        }

    previous_extreme = _positive(plan.get("favorable_extreme")) or entry
    favorable_extreme = (
        max(previous_extreme, price)
        if side == "long"
        else min(previous_extreme, price)
    )
    favorable_move = (
        favorable_extreme - entry
        if side == "long"
        else entry - favorable_extreme
    )
    peak_r = max(float(plan.get("peak_r_multiple") or 0.0), favorable_move / risk_distance)
    completed = set(plan.get("completed_stages", []))
    break_even_activation = float(getattr(settings, "break_even_activation_r", 1.20))
    if volatility_regime == "expansion":
        break_even_activation = max(
            break_even_activation,
            float(getattr(settings, "expansion_break_even_activation_r", 1.50)),
        )
    elif volatility_regime == "isolated_spike":
        break_even_activation = max(
            break_even_activation,
            float(getattr(settings, "isolated_spike_break_even_activation_r", 1.60)),
        )
    break_even_ready = (
        peak_r >= break_even_activation
        or "TP1" in completed
    )
    structure_ready = (
        peak_r >= float(getattr(settings, "structure_trail_activation_r", 2.0))
        or "TP2" in completed
    )
    runner_ready = (
        peak_r >= float(getattr(settings, "runner_trail_activation_r", 2.5))
        or "TP3" in completed
    )

    candidate = current_stop
    source = "initial_invalidation"
    phase = "INITIAL_RISK"
    fee_distance = entry * float(getattr(settings, "estimated_round_trip_fee_pct", 0.0012))
    fee_distance *= float(getattr(settings, "break_even_fee_buffer_multiple", 1.25))
    if break_even_ready:
        fee_break_even = entry + fee_distance if side == "long" else entry - fee_distance
        candidate = max(candidate, fee_break_even) if side == "long" else min(candidate, fee_break_even)
        source = "fee_covered_break_even"
        phase = "FEE_BREAK_EVEN"

    state = str(plan.get("market_state", "normal")).lower()
    if volatility_regime == "isolated_spike":
        structure_buffer = 1.50
    elif volatility_regime == "expansion" or state in {"high_volatility", "extreme"}:
        structure_buffer = 1.35
    elif state in {"range", "low_volatility", "mixed"}:
        structure_buffer = 1.00
    else:
        structure_buffer = 1.10
    if structure_ready:
        structure_level = _positive(
            context.get("recent_low15" if side == "long" else "recent_high15")
        )
        if structure_level is not None:
            structure_stop = (
                structure_level - structure_buffer * management_atr
                if side == "long"
                else structure_level + structure_buffer * management_atr
            )
            live_side = structure_stop < price if side == "long" else structure_stop > price
            if live_side:
                candidate = (
                    max(candidate, structure_stop)
                    if side == "long"
                    else min(candidate, structure_stop)
                )
                source = "15m_structure_trail"
        phase = "STRUCTURE_TRAILING"

    if runner_ready:
        if volatility_regime == "isolated_spike":
            trail_atr = max(
                4.0,
                float(getattr(settings, "high_volatility_trailing_atr", 3.5)),
            )
        elif volatility_regime == "expansion" or state in {"high_volatility", "extreme"}:
            trail_atr = max(
                3.8,
                float(getattr(settings, "high_volatility_trailing_atr", 3.5)),
            )
        elif state in {"range", "low_volatility", "mixed"}:
            trail_atr = max(
                2.6,
                float(getattr(settings, "range_trailing_atr", 2.2)),
            )
        else:
            trail_atr = max(
                3.2,
                float(getattr(settings, "trend_trailing_atr", 3.0)),
            )
        chandelier = (
            favorable_extreme - trail_atr * management_atr
            if side == "long"
            else favorable_extreme + trail_atr * management_atr
        )
        live_side = chandelier < price if side == "long" else chandelier > price
        if live_side:
            candidate = max(candidate, chandelier) if side == "long" else min(candidate, chandelier)
            source = f"{volatility_regime}_{state}_chandelier"
        phase = "RUNNER_TRAILING"

    return {
        "candidate_stop": candidate,
        "phase": phase,
        "favorable_extreme": favorable_extreme,
        "peak_r_multiple": peak_r,
        "trail_source": source,
        "management_atr": management_atr,
        "volatility_regime": volatility_regime,
        "break_even_activation_r": break_even_activation,
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


def adaptive_operational_leverage(
    exchange_leverage: float,
    entry: float,
    stop: float,
    settings: Any,
    minimum_leverage: float = 1.0,
) -> float:
    """Return an exchange-valid integer leverage below every safety cap."""
    stop_pct = abs(entry - stop) / entry if entry > 0 else 0.0
    if stop_pct <= 0:
        raise TradingRiskError("INVALID_STOP", "cannot select leverage without a valid stop")
    distance_cap = 1.0 / (
        stop_pct * max(1.0, float(settings.liquidation_distance_stop_multiple))
    )
    raw_cap = min(
        float(exchange_leverage),
        float(settings.max_operational_leverage),
        distance_cap,
    )
    if not isfinite(raw_cap) or raw_cap <= 0:
        raise TradingRiskError(
            "INVALID_LEVERAGE",
            "exchange or risk-model leverage is not a finite positive value",
        )
    minimum_integer = int(
        Decimal(str(minimum_leverage)).to_integral_value(rounding=ROUND_CEILING)
    )
    leverage_integer = int(Decimal(str(raw_cap)).to_integral_value(rounding=ROUND_DOWN))
    if leverage_integer < max(1, minimum_integer):
        raise TradingRiskError(
            "LEVERAGE_SAFETY_UNAVAILABLE",
            (
                f"safe leverage cap {raw_cap:.4f}x is below the contract minimum "
                f"{max(1, minimum_integer)}x"
            ),
        )
    return float(leverage_integer)


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
    actual_notional = size * Decimal(str(price)) * Decimal(str(multiplier))
    minimum_notional = Decimal(
        str(getattr(contract_info, "order_notional_min", None) or 0)
    )
    if minimum_notional and actual_notional < minimum_notional:
        raise TradingRiskError(
            "ORDER_NOTIONAL_TOO_SMALL",
            (
                f"rounded order notional {actual_notional} is below Bitget minimum "
                f"{minimum_notional}"
            ),
        )
    text = _decimal_text(size)
    return text, float(actual_notional)


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


def planned_take_profit_sizes(
    plan: dict[str, Any],
    live_size: str | float | Decimal,
) -> dict[str, str]:
    """Preserve original TP allocations as the position shrinks.

    Percentages belong to the original filled position, not repeatedly to the
    remaining position. Re-applying 30% to 70% after TP1 would silently turn
    TP2 into 21% of the original trade and leave an unintended oversized
    runner. This allocator also caps the total pending TP quantity below the
    live position and explicitly reserves the configured runner.
    """
    live = abs(Decimal(str(live_size)))
    initial = abs(
        Decimal(
            str(
                plan.get("initial_position_size")
                or plan.get("entry_size")
                or live
            )
        )
    )
    if live <= 0 or initial <= 0:
        return {}
    targets = plan.get("take_profits", [])
    configured_total = sum(Decimal(str(target.get("percent") or 0)) for target in targets)
    runner_fraction = Decimal(
        str(plan.get("runner_percent") if plan.get("runner_percent") is not None else max(Decimal("0"), Decimal("1") - configured_total))
    )
    runner_reserve = min(live, initial * max(Decimal("0"), runner_fraction))
    available = max(Decimal("0"), live - runner_reserve)
    completed = set(plan.get("completed_stages", []))
    result: dict[str, str] = {}
    close_side = "short" if str(plan.get("side")).lower() == "long" else "long"
    step = abs(Decimal(str(plan.get("size_step") or "0.00000001")))
    minimum = abs(Decimal(str(plan.get("order_size_min") or 0)))
    for target in targets:
        stage = str(target.get("stage") or "")
        if not stage or stage in completed or available <= 0:
            continue
        desired = initial * Decimal(str(target.get("percent") or 0))
        quantity = min(desired, available)
        if step > 0:
            quantity = (
                quantity / step
            ).to_integral_value(rounding=ROUND_DOWN) * step
        if quantity <= 0 or (minimum > 0 and quantity < minimum):
            continue
        result[stage] = signed_size(close_side, quantity)
        available -= quantity
    return result

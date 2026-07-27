from typing import Any

WEIGHTS = {
    "environment": 12,
    "breakout": 18,
    "structure": 8,
    "turnover": 10,
    "oi": 10,
    "dmi": 5,
    "adx": 7,
    "mfi": 5,
    "ema": 5,
    "vwap": 4,
    "boll": 4,
    "active_flow": 6,
    "pullback": 6,
}

TACTICAL_WEIGHTS = {
    "breakout": 25,
    "structure_30m": 15,
    "turnover": 15,
    "dmi": 10,
    "adx": 10,
    "trend_15m": 10,
    "trend_5m": 10,
    "active_flow": 5,
}


def _value(features: dict[str, Any], direction: str) -> dict[str, float | None]:
    f30 = features.get("30m", {})
    bullish = direction == "long"
    sign = 1 if bullish else -1
    env = 1.0 if features.get("market_state") == ("bullish" if bullish else "bearish") else 0.35 if features.get("market_state") == "mixed" else 0.0
    breakout_name = "breakout" if bullish else "breakdown"
    breakout = 1.0 if features.get("breakout", {}).get(breakout_name) else 0.0
    structure = 1.0 if (f30.get("ema20") is not None and f30.get("ema50") is not None and (f30["ema20"] > f30["ema50"]) == bullish) else 0.2
    ratio = features.get("turnover", {}).get("turnover_ratio")
    turnover = None if ratio is None else min(1.0, max(0.0, (ratio - 0.5) / 2.5))
    oi_change = features.get("oi", {}).get("oi_change_30m_pct")
    oi = None if oi_change is None else min(1.0, max(0.0, (abs(oi_change) + 1) / 8))
    plus, minus = f30.get("plus_di"), f30.get("minus_di")
    dmi = None if plus is None or minus is None else min(1.0, max(0.0, ((plus - minus) * sign + 20) / 40))
    adx = None if f30.get("adx") is None else min(1.0, max(0.0, (f30["adx"] - 12) / 35))
    mfi_value = f30.get("mfi")
    mfi_signal = None if mfi_value is None else min(1.0, max(0.0, ((mfi_value - 45) * sign + 35) / 70))
    e20, e50, e200 = f30.get("ema20"), f30.get("ema50"), f30.get("ema200")
    ema_signal = None if None in (e20, e50, e200) else 1.0 if (e20 > e50 > e200) == bullish or (e20 < e50 < e200) == (not bullish) else 0.25
    close, vwap_value = f30.get("close"), f30.get("vwap")
    vwap_signal = None if close is None or vwap_value is None else min(1.0, max(0.0, (close - vwap_value) * sign / (abs(close) * 0.01) / 3 + 0.5))
    bw = f30.get("boll_bandwidth")
    boll_signal = None if bw is None else min(1.0, max(0.0, bw / 0.2))
    active_flow = features.get("active_flow", {})
    flow_ratio = active_flow.get("buy_sell_ratio") if isinstance(active_flow, dict) else None
    active_signal = None if flow_ratio is None else min(1.0, max(0.0, ((flow_ratio - 1) * sign + 1) / 2))
    pullback = 1.0 if features.get("pullback15", {}).get("state") in {"pullback", "rebound"} else 0.35
    return {
        "environment": env,
        "breakout": breakout,
        "structure": structure,
        "turnover": turnover,
        "oi": oi,
        "dmi": dmi,
        "adx": adx,
        "mfi": mfi_signal,
        "ema": ema_signal,
        "vwap": vwap_signal,
        "boll": boll_signal,
        "active_flow": active_signal,
        "pullback": pullback,
    }


def score_direction(features: dict[str, Any], direction: str) -> tuple[float, float, list[str]]:
    values = _value(features, direction)
    raw = 0.0
    available_weight = 0.0
    reasons: list[str] = []
    for name, weight in WEIGHTS.items():
        value = values[name]
        if value is None:
            continue
        available_weight += weight
        raw += value * weight
        if value >= 0.7:
            reasons.append(f"{direction}:{name}")
    return ((raw / available_weight * 100) if available_weight else 0.0, available_weight, reasons)


def tactical_score_direction(
    features: dict[str, Any], direction: str
) -> tuple[float, bool, list[str]]:
    """Score an exceptional 30m move against the prevailing 4h regime.

    A tactical setup is intentionally stricter than an ordinary trend score.
    It requires agreement across 30m structure, 15m continuation and a 5m
    execution trend; optional active flow can improve the score but cannot
    rescue a structurally invalid signal.
    """
    bullish = direction == "long"
    breakout_key = "breakout" if bullish else "breakdown"
    f30 = features.get("30m", {})
    f15 = features.get("15m", {})
    f5 = features.get("5m", {})
    breakout = bool(features.get("breakout", {}).get(breakout_key))
    turnover_ratio = features.get("turnover", {}).get("turnover_ratio")
    adx_value = f30.get("adx")

    def trend(frame: dict[str, Any], *, require_vwap: bool = False) -> bool:
        close = frame.get("close")
        e20 = frame.get("ema20")
        e50 = frame.get("ema50")
        if close is None or e20 is None or e50 is None:
            return False
        close = float(close)
        e20 = float(e20)
        e50 = float(e50)
        ordered = close > e20 > e50 if bullish else close < e20 < e50
        vwap_value = frame.get("vwap")
        if require_vwap and vwap_value is not None:
            ordered = ordered and (close > vwap_value if bullish else close < vwap_value)
        return bool(ordered)

    structure_30m = trend(f30)
    trend_15m = trend(f15)
    trend_5m = trend(f5, require_vwap=True)
    plus, minus = f30.get("plus_di"), f30.get("minus_di")
    dmi = (
        False
        if plus is None or minus is None
        else (plus > minus if bullish else minus > plus)
    )
    turnover = (
        0.0
        if turnover_ratio is None
        else min(1.0, max(0.0, float(turnover_ratio) / 2.0))
    )
    adx_score = (
        0.0
        if adx_value is None
        else min(1.0, max(0.0, (float(adx_value) - 14.0) / 26.0))
    )
    flow = features.get("active_flow", {})
    flow_ratio = flow.get("buy_sell_ratio") if isinstance(flow, dict) else None
    if flow_ratio is None:
        active_flow = 0.5
    elif bullish:
        active_flow = min(1.0, max(0.0, float(flow_ratio) / 1.5))
    else:
        active_flow = min(1.0, max(0.0, (1.5 - float(flow_ratio)) / 1.0))
    values = {
        "breakout": float(breakout),
        "structure_30m": float(structure_30m),
        "turnover": turnover,
        "dmi": float(dmi),
        "adx": adx_score,
        "trend_15m": float(trend_15m),
        "trend_5m": float(trend_5m),
        "active_flow": active_flow,
    }
    score = sum(values[name] * weight for name, weight in TACTICAL_WEIGHTS.items())
    reasons = [
        f"{direction}:tactical_{name}"
        for name, value in values.items()
        if value >= 0.7
    ]
    hard_valid = bool(
        breakout and structure_30m and trend_15m and trend_5m and dmi
    )
    return score, hard_valid, reasons

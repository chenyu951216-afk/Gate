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

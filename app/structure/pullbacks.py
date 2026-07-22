def pullback_signal(
    close: float, ema20: float | None, ema50: float | None, vwap: float | None, atr_value: float | None
) -> dict[str, float | str | bool | None]:
    if atr_value is None or atr_value <= 0 or ema20 is None:
        return {"available": False, "state": "unavailable", "distance_atr": None}
    distance = abs(close - ema20) / atr_value
    aligned = close >= ema20 if ema50 is None else (close >= ema20 >= ema50)
    near = distance <= 1.5 and (vwap is None or abs(close - vwap) / atr_value <= 2.0)
    return {
        "available": True,
        "state": "pullback" if near and aligned else "rebound" if near else "extended",
        "distance_atr": distance,
    }

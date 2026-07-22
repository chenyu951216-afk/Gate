from typing import Any


def execute_trade(
    direction: str,
    entry: float,
    future_bars: list[dict[str, float]],
    atr_value: float | None,
    holding_bars: int,
    stop_atr: float | None,
    take_atr: float | None,
    fee_pct: float,
    slippage_pct: float,
) -> dict[str, Any]:
    if entry <= 0 or not future_bars:
        return {"status": "unavailable", "pnl_pct": None}
    slip = slippage_pct / 100
    actual_entry = entry * (1 + slip if direction == "long" else 1 - slip)
    stop = actual_entry - atr_value * stop_atr if direction == "long" and atr_value and stop_atr else actual_entry + atr_value * stop_atr if direction == "short" and atr_value and stop_atr else None
    take = actual_entry + atr_value * take_atr if direction == "long" and atr_value and take_atr else actual_entry - atr_value * take_atr if direction == "short" and atr_value and take_atr else None
    selected = future_bars[:holding_bars]
    exit_price = selected[-1]["close"]
    exit_reason = "holding_end"
    mfe = 0.0
    mae = 0.0
    for bar in selected:
        high, low = bar["high"], bar["low"]
        if direction == "long":
            mfe = max(mfe, (high - actual_entry) / actual_entry * 100)
            mae = min(mae, (low - actual_entry) / actual_entry * 100)
            if stop is not None and low <= stop:
                exit_price, exit_reason = stop, "atr_stop"
                break
            if take is not None and high >= take:
                exit_price, exit_reason = take, "atr_take"
                break
        else:
            mfe = max(mfe, (actual_entry - low) / actual_entry * 100)
            mae = min(mae, (actual_entry - high) / actual_entry * 100)
            if stop is not None and high >= stop:
                exit_price, exit_reason = stop, "atr_stop"
                break
            if take is not None and low <= take:
                exit_price, exit_reason = take, "atr_take"
                break
    gross = ((exit_price - actual_entry) / actual_entry if direction == "long" else (actual_entry - exit_price) / actual_entry) * 100
    net = gross - 2 * fee_pct - 2 * slippage_pct
    return {"status": "closed", "entry": actual_entry, "exit": exit_price, "exit_reason": exit_reason, "gross_pnl_pct": gross, "pnl_pct": net, "mfe_pct": mfe, "mae_pct": mae}


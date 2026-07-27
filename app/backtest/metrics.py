import math
from typing import Any


def calculate_metrics(trades: list[dict[str, Any]]) -> dict[str, float | int | None]:
    returns = [float(item["pnl_pct"]) for item in trades if item.get("pnl_pct") is not None]
    if not returns:
        return {"trades": 0, "win_rate_pct": None, "total_return_pct": None, "average_return_pct": None, "max_drawdown_pct": None, "profit_factor": None}
    equity = 100.0
    peak = equity
    drawdown = 0.0
    gains = 0.0
    losses = 0.0
    for value in returns:
        equity *= 1 + value / 100
        peak = max(peak, equity)
        drawdown = min(drawdown, (equity - peak) / peak * 100)
        gains += max(0, value)
        losses += max(0, -value)
    return {"trades": len(returns), "win_rate_pct": sum(value > 0 for value in returns) / len(returns) * 100, "total_return_pct": equity - 100, "average_return_pct": sum(returns) / len(returns), "max_drawdown_pct": abs(drawdown), "profit_factor": gains / losses if losses else math.inf}


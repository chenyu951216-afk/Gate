import uuid
from datetime import datetime, timezone
from typing import Any

from app.backtest.execution import execute_trade
from app.backtest.metrics import calculate_metrics
from app.backtest.walk_forward import split_walk_forward


class BacktestService:
    def __init__(self, repository: Any, settings: Any, gate: Any | None = None):
        self.repository = repository
        self.settings = settings
        self.gate = gate

    async def run(self, job: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        trades: list[dict[str, Any]] = []
        diagnostics: list[str] = []
        for timepoint in job.get("results", []):
            rankings = timepoint.get("rankings", {})
            for ranking_type in request.get("ranking_types", ["combined"]):
                for item in rankings.get(ranking_type, [])[: request.get("top_n", 10)]:
                    bars = item.get("metrics", {}).get("future_bars", [])
                    if not bars and self.gate and item.get("contract") and timepoint.get("aligned_time"):
                        try:
                            entry_time = datetime.fromisoformat(str(timepoint["aligned_time"]))
                            entry_ts = int(entry_time.timestamp())
                            holding = request.get("holding_bars", 4)
                            raw_bars = await self.gate.rest.get_candlesticks(
                                item["contract"], "30m", from_ts=entry_ts + 1800,
                                to_ts=entry_ts + (holding + 1) * 1800,
                            )
                            from app.gate.normalizer import normalize_candles
                            bars = [{"open": candle.open, "high": candle.high, "low": candle.low, "close": candle.close} for candle in normalize_candles(raw_bars)]
                        except Exception as exc:
                            diagnostics.append(f"{item.get('contract')}: future bars unavailable: {type(exc).__name__}")
                    if not bars:
                        diagnostics.append(f"{item.get('contract')}: future bars unavailable")
                        continue
                    f30 = item.get("metrics", {}).get("30m", {})
                    trade = execute_trade(item["direction"], f30.get("close", 0), bars, f30.get("atr"), request.get("holding_bars", 4), request.get("stop_atr", 2.0), request.get("take_atr", 3.0), request.get("fee_pct", self.settings.backtest_default_fee_pct), request.get("slippage_pct", self.settings.backtest_default_slippage_pct))
                    trade.update({"contract": item.get("contract"), "ranking_type": ranking_type, "entry_time": timepoint.get("aligned_time")})
                    trades.append(trade)
        train, test = split_walk_forward(trades)
        result = {"run_id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc), "parameters": request, "metrics": {**calculate_metrics(trades), "train_trades": len(train), "out_of_sample_trades": len(test)}, "trades": trades, "diagnostics": diagnostics}
        await self.repository.save_backtest(result)
        return result

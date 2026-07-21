import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from app.scanner.analyzer import analyze_market
from app.scanner.collector import MarketCollector
from app.scanner.ranking import build_rankings, rank_analysis
from app.scanner.universe import build_universe

logger = logging.getLogger(__name__)


class ScanService:
    def __init__(self, gate_client: Any, repository: Any, settings: Any, notifier: Any | None = None, trading: Any | None = None):
        self.gate = gate_client
        self.repository = repository
        self.settings = settings
        self.notifier = notifier
        self.trading = trading
        self._lock = asyncio.Lock()

    async def run(self, dry_run: bool = False, notify_discord: bool | None = None, top_n: int = 10) -> dict[str, Any]:
        if self._lock.locked():
            return {"status": "already_running", "scan_id": "", "started_at": datetime.now(timezone.utc)}
        async with self._lock:
            scan_id = uuid.uuid4().hex
            started = datetime.now(timezone.utc)
            start_clock = time.monotonic()
            try:
                contracts, tickers = await asyncio.gather(
                    self.gate.rest.get_contracts(), self.gate.rest.get_tickers()
                )
                universe = build_universe(contracts, tickers, self.settings.blacklist, self.settings)
                collector = MarketCollector(self.gate.rest, self.settings)
                collected = await collector.collect_batch(universe)
                analyses: list[dict[str, Any]] = []
                suppressed: list[dict[str, Any]] = []
                scan_errors: list[dict[str, Any]] = []
                for data in collected:
                    try:
                        analysis = analyze_market(data, self.settings.min_30m_candles, self.settings.min_4h_candles)
                        analysis["contract"] = data["info"].name
                        item = rank_analysis(analysis, self.settings, top_n)
                        if item is not None:
                            analyses.append(item)
                            if not item["qualifies"]:
                                suppressed.append({"contract": data["info"].name, "risk_flags": item["risk_flags"], "score": item["ranking_score"]})
                    except Exception as exc:
                        scan_errors.append({"contract": data["info"].name, "error": type(exc).__name__})
                rankings = build_rankings(analyses, top_n)
                finished = datetime.now(timezone.utc)
                result: dict[str, Any] = {
                    "scan_id": scan_id,
                    "status": "dry_run" if dry_run else "completed",
                    "started_at": started,
                    "finished_at": finished,
                    "elapsed_seconds": time.monotonic() - start_clock,
                    "universe_total": len(universe),
                    "excluded_count": max(0, len(contracts) - len(universe)),
                    "successful_count": len(analyses),
                    "error_count": len(scan_errors),
                    "dry_run": dry_run,
                    "rankings": rankings,
                    "diagnostics": {"suppressed": suppressed, "errors": scan_errors, "source": "Gate official REST v4"},
                }
                if not dry_run:
                    await self.repository.save_scan(result)
                    if self.notifier and (notify_discord if notify_discord is not None else True):
                        await self.notifier.send_scan(result)
                    if self.trading:
                        try:
                            result["trading"] = await self.trading.process_scan(result)
                        except Exception as exc:
                            logger.exception("automatic order processing failed")
                            result["trading"] = {"status": "failed", "error": type(exc).__name__}
                return result
            except Exception as exc:
                failure_result: dict[str, Any] = {
                    "scan_id": scan_id,
                    "status": "failed",
                    "started_at": started,
                    "finished_at": datetime.now(timezone.utc),
                    "elapsed_seconds": time.monotonic() - start_clock,
                    "universe_total": 0,
                    "excluded_count": 0,
                    "successful_count": 0,
                    "error_count": 1,
                    "dry_run": dry_run,
                    "rankings": {"combined": [], "long": [], "short": []},
                    "diagnostics": {"error_type": type(exc).__name__, "error": str(exc)},
                }
                await self.repository.save_scan(failure_result)
                logger.exception("scan failed")
                return failure_result

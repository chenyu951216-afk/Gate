from contextlib import asynccontextmanager
import asyncio
import logging

from fastapi import FastAPI

from app.config import get_settings
from app.coinglass.client import CoinGlassClient
from app.database.repository import MemoryRepository, PostgresRepository
from app.database.session import create_database, initialize_database
from app.gate.client import GateClient
from app.gate.rest_client import GateRestClient
from app.gate.websocket_client import GateFuturesWebsocket
from app.logging_config import configure_logging
from app.notifications.delivery import NotificationService
from app.replay.engine import ReplayService
from app.scanner.service import ScanService
from app.backtest.engine import BacktestService
from app.dependencies import AppState
from app.scheduler import ScanScheduler
from app.trading.service import TradingService

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        engine, session_factory = create_database(settings.database_url)
    except Exception:
        logger.exception("invalid DATABASE_URL; expected a PostgreSQL connection string")
        raise
    if engine:
        retries = max(1, int(settings.database_startup_retries))
        for attempt in range(1, retries + 1):
            try:
                await initialize_database(engine)
                break
            except Exception:
                logger.exception(
                    "database initialization attempt %s/%s failed; verify DATABASE_URL and Zeabur PostgreSQL networking",
                    attempt,
                    retries,
                )
                if attempt == retries:
                    await engine.dispose()
                    raise
                await asyncio.sleep(float(settings.database_startup_retry_delay_seconds))
    repository = PostgresRepository(session_factory) if session_factory else MemoryRepository()
    rest = GateRestClient(settings)
    gate = GateClient(rest, GateFuturesWebsocket(settings.gate_ws_url))
    coinglass = CoinGlassClient(settings)
    notifier = NotificationService(settings, repository)
    trading = TradingService(gate, repository, settings, notifier)
    scanner = ScanService(gate, repository, settings, notifier, trading, coinglass)
    replay = ReplayService(gate, repository, settings, notifier)
    backtest = BacktestService(repository, settings, gate)
    scheduler = ScanScheduler(scanner, settings) if settings.scheduler_enabled else None
    app.state.services = AppState(settings, gate, repository, notifier, scanner, replay, backtest, trading, scheduler)
    await trading.start()
    if scheduler:
        scheduler.start()
    try:
        yield
    finally:
        if scheduler:
            await scheduler.stop()
        await trading.stop()
        await coinglass.close()
        await gate.close()
        if engine:
            await engine.dispose()

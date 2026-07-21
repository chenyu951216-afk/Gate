from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    engine, session_factory = create_database(settings.database_url)
    await initialize_database(engine)
    repository = PostgresRepository(session_factory) if session_factory else MemoryRepository()
    rest = GateRestClient(settings)
    gate = GateClient(rest, GateFuturesWebsocket(settings.gate_ws_url))
    notifier = NotificationService(settings, repository)
    trading = TradingService(gate, repository, settings, notifier)
    scanner = ScanService(gate, repository, settings, notifier, trading)
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
        await gate.close()
        if engine:
            await engine.dispose()

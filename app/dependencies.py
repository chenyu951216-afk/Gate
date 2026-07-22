from dataclasses import dataclass
from typing import Any

from fastapi import Request

from app.backtest.engine import BacktestService
from app.config import Settings
from app.notifications.delivery import NotificationService
from app.replay.engine import ReplayService
from app.scanner.service import ScanService
from app.trading.service import TradingService


@dataclass
class AppState:
    settings: Settings
    # Kept as `gate` for internal compatibility with the existing scanner and
    # trading service; at runtime this is a BitgetClient.
    gate: Any
    repository: Any
    notifier: NotificationService
    scanner: ScanService
    replay: ReplayService
    backtest: BacktestService
    trading: TradingService
    scheduler: Any | None = None


def state_from_request(request: Request) -> AppState:
    return request.app.state.services


async def require_bearer(request: Request) -> None:
    state = state_from_request(request)
    header = request.headers.get("Authorization", "")
    valid_tokens = {
        token
        for token in (
            state.settings.admin_bearer_token,
            state.settings.manual_scan_token,
            state.settings.trading_control_token,
        )
        if token
    }
    if not valid_tokens or header not in {f"Bearer {token}" for token in valid_tokens}:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Bearer token required")

from fastapi import APIRouter

from app.api import backtests, contracts, dashboard, health, notifications, rankings, replay, scans, trading

router = APIRouter()
router.include_router(health.router)
router.include_router(dashboard.router)
router.include_router(rankings.router)
router.include_router(contracts.router)
router.include_router(scans.router)
router.include_router(replay.router)
router.include_router(backtests.router)
router.include_router(notifications.router)
router.include_router(trading.router)

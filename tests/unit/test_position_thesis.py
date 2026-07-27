from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.trading.position_thesis import (
    assess_closed_15m_thesis,
    assess_closed_5m_exit,
)
from app.trading.risk import managed_stop_candidate
from app.trading.service import TradingService


def _decisive_long_15m(timestamp: str) -> dict:
    return {
        "closed_timestamp15": timestamp,
        "last_close15": 90,
        "atr15": 2,
        "ema2015": 94,
        "ema5015": 98,
        "vwap15": 95,
        "recent_low15": 94,
        "recent_high15": 102,
        "plus_di15": 10,
        "minus_di15": 30,
        "adx15": 30,
        "mfi15": 25,
        "turnover_ratio15": 1.5,
        "ema20_slope_atr15": -0.5,
        "below_trend_count15": 3,
    }


def test_15m_reversal_requires_multiple_independent_evidence_groups():
    settings = Settings(_env_file=None)
    context = _decisive_long_15m("2026-07-27T12:45:00+00:00")
    assessment = assess_closed_15m_thesis("long", context, settings)
    assert assessment["decisive"] is True
    assert {
        "15m_structure_break",
        "15m_ema_stack_reversed",
        "15m_dmi_adx_reversed",
        "15m_volume_mfi_reversal",
    }.issubset(assessment["evidence"])


def test_5m_exit_cannot_define_the_thesis_but_can_execute_after_arming():
    settings = Settings(_env_file=None)
    context = {
        "closed_timestamp5": "2026-07-27T12:50:00+00:00",
        "last_close5": 95,
        "atr5": 1,
        "ema205": 97,
        "ema505": 98,
        "vwap5": 97,
        "recent_low5": 96,
        "recent_high5": 101,
        "plus_di5": 10,
        "minus_di5": 25,
        "adx5": 25,
    }
    assessment = assess_closed_5m_exit(
        side="long",
        context=context,
        live_price=95,
        current_r=-0.5,
        recovery_target=99.5,
        settings=settings,
    )
    assert assessment["decisive"] is True
    assert assessment["target_reached"] is False


def test_5m_non_decisive_exit_needs_distinct_closed_candles():
    plan: dict = {}
    first = {
        "candidate": True,
        "decisive": False,
        "observation_key": "2026-07-27T12:50:00+00:00",
    }
    result1 = TradingService._update_5m_exit_confirmation(plan, first, 2)
    duplicate = TradingService._update_5m_exit_confirmation(plan, first, 2)
    second = {
        **first,
        "observation_key": "2026-07-27T12:55:00+00:00",
    }
    result2 = TradingService._update_5m_exit_confirmation(plan, second, 2)
    assert result1["confirmed"] is False
    assert duplicate["confirmations"] == 1
    assert result2["confirmed"] is True


def test_live_mark_spike_cannot_activate_break_even_without_15m_close():
    settings = Settings(_env_file=None)
    trail = managed_stop_candidate(
        plan={
            "side": "long",
            "current_stop": 98,
            "initial_risk_distance": 2,
            "atr15": 1,
            "market_state": "bullish",
            "completed_stages": [],
        },
        context={
            "atr15": 1,
            "last_close15": 100.5,
            "last_high15": 104,
        },
        price=104,
        entry=100,
        settings=settings,
    )
    assert trail["candidate_stop"] == 98
    assert trail["peak_r_multiple"] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_position_context_fetches_only_closed_15m_and_5m_data():
    settings = Settings(_env_file=None)

    class Rest:
        def __init__(self):
            self.intervals: list[str] = []

        async def get_ticker(self, contract):
            return {"contract": contract, "mark_price": "100", "last": "100"}

        async def get_candlesticks(self, contract, interval, limit):
            self.intervals.append(interval)
            seconds = 900 if interval == "15m" else 300
            end = datetime.now(timezone.utc)
            latest = int(end.timestamp()) // seconds * seconds - seconds
            return [
                {
                    "t": latest - seconds * (limit - index - 1),
                    "o": "100",
                    "h": str(101 + index * 0.001),
                    "l": str(99 + index * 0.001),
                    "c": str(100 + index * 0.001),
                    "v": "1000",
                    "sum": "100000",
                }
                for index in range(limit)
            ]

    rest = Rest()
    service = TradingService(
        SimpleNamespace(rest=rest),
        SimpleNamespace(),
        settings,
    )
    info = SimpleNamespace(quanto_multiplier=0.001)
    _, context = await service._market_context("ALT_USDT", info)
    assert set(rest.intervals) == {"15m", "5m"}
    assert "30m" not in rest.intervals
    assert context["source"] == "closed_15m_position_monitor"
    assert context["integrity15"]["complete"] is True
    assert context["integrity5"]["complete"] is True

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.config import Settings
from app.market_sessions import contract_session_status
from app.scanner.integrity import candle_integrity
from app.scanner.lifecycle import annotate_signal_lifecycle
from app.scanner.ranking import rank_analysis
from app.scanner.standards import adaptive_scan_standards
from app.trading.service import TradingService


def test_default_altcoin_turnover_is_five_million():
    assert Settings(_env_file=None).min_24h_turnover_usdt == 5_000_000


def test_us_stock_gate_uses_real_regular_session_and_holiday():
    settings = Settings(_env_file=None)
    opened = contract_session_status(
        "AAPLX_USDT",
        "stocks",
        settings,
        at=datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc),
    )
    closed = contract_session_status(
        "AAPLX_USDT",
        "stocks",
        settings,
        at=datetime(2026, 7, 27, 21, 0, tzinfo=timezone.utc),
    )
    holiday = contract_session_status(
        "AAPLX_USDT",
        "stocks",
        settings,
        at=datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc),
    )
    assert opened["is_open"] is True
    assert closed["is_open"] is False
    assert holiday["is_open"] is False


def test_stock_calendar_override_is_applied():
    settings = Settings(
        _env_file=None,
        stock_calendar_overrides="ANTA_USDT:XHKG",
    )
    status = contract_session_status(
        "ANTA_USDT",
        "stocks",
        settings,
        at=datetime(2026, 7, 27, 2, 0, tzinfo=timezone.utc),
    )
    assert status["calendar"] == "XHKG"
    assert status["is_open"] is True


def test_candle_integrity_rejects_gap_and_stale_history():
    start = datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc)
    candles = [
        SimpleNamespace(timestamp=start + timedelta(minutes=30 * index))
        for index in range(10)
        if index != 5
    ]
    report = candle_integrity(
        candles,
        "30m",
        8,
        as_of=start + timedelta(hours=8),
    )
    assert "historical_data_gap" in report["problems"]
    assert "stale_data" in report["problems"]
    assert report["complete"] is False


def test_adaptive_standards_relieve_confirmed_expansion_not_spike():
    expansion = adaptive_scan_standards(
        {"regime": "expansion"},
        base_ranking_score=55,
        trend_score_relief=3,
    )
    spike = adaptive_scan_standards(
        {"regime": "isolated_spike"},
        base_ranking_score=55,
        trend_score_relief=3,
    )
    assert expansion["minimum_ranking_score"] == 52
    assert expansion["min_turnover_ratio"] < spike["min_turnover_ratio"]
    assert expansion["breakout_atr_multiple"] < spike["breakout_atr_multiple"]


def _tactical_analysis() -> dict:
    return {
        "contract": "ALT_USDT",
        "contract_type": "",
        "market_state": "bullish",
        "signal_state": "breakdown",
        "missing_data": [],
        "errors": [],
        "features": {
            "ticker": {
                "turnover_usdt": 10_000_000,
                "bid": 100.0,
                "ask": 100.01,
            },
            "30m": {
                "close": 90.0,
                "ema20": 92.0,
                "ema50": 95.0,
                "ema200": 100.0,
                "vwap": 93.0,
                "plus_di": 10.0,
                "minus_di": 30.0,
                "adx": 30.0,
                "mfi": 35.0,
                "boll_bandwidth": 0.1,
            },
            "15m": {
                "close": 89.0,
                "ema20": 91.0,
                "ema50": 94.0,
                "vwap": 92.0,
            },
            "5m": {
                "close": 88.0,
                "ema20": 89.0,
                "ema50": 91.0,
                "vwap": 90.0,
            },
            "turnover": {"turnover_ratio": 2.0},
            "oi": {"oi_change_30m_pct": 2.0},
            "active_flow": {"buy_sell_ratio": 0.5},
            "breakout": {"breakout": False, "breakdown": True},
            "pullback15": {"state": "pullback"},
            "divergence": {},
            "special_pattern": None,
            "scan_standards": {
                "minimum_direction_score": 60,
                "minimum_ranking_score": 55,
            },
        },
    }


def test_strong_counter_4h_setup_becomes_tradeable_tactical_signal():
    item = rank_analysis(_tactical_analysis(), Settings(_env_file=None))
    assert item is not None
    assert item["qualifies"] is True
    assert item["direction"] == "short"
    assert item["signal_class"] == "tactical"
    assert item["signal_horizon"] == "30m_tactical"


def test_primary_history_gap_is_fatal_even_with_strong_signal():
    analysis = _tactical_analysis()
    analysis["errors"] = ["historical_data_gap"]
    item = rank_analysis(analysis, Settings(_env_file=None))
    assert item is not None
    assert item["qualifies"] is False


def test_signal_lifecycle_does_not_change_qualification():
    item = {
        "contract": "ALT_USDT",
        "direction": "long",
        "qualifies": True,
    }
    rankings = {
        "combined": [item],
        "long": [item],
        "short": [],
        "tactical": [],
    }
    history = [
        {
            "rankings": {
                "combined": [
                    {"contract": "ALT_USDT", "direction": "long"}
                ]
            }
        }
    ]
    counts = annotate_signal_lifecycle(rankings, history)
    assert item["signal_lifecycle"] == "PERSISTING"
    assert item["consecutive_qualified_scans"] == 2
    assert item["qualifies"] is True
    assert counts == {"PERSISTING": 1}


def test_five_minute_entry_uses_structure_and_respects_distance_cap():
    settings = Settings(_env_file=None)
    price, audit = TradingService._five_minute_entry_price(
        ticker={"highest_bid": "100", "lowest_ask": "100.1"},
        metrics={
            "5m": {
                "atr": 2.0,
                "ema20": 99.4,
                "ema50": 98.8,
                "vwap": 99.2,
                "recent_low": 97.0,
            }
        },
        side="long",
        mark_price=100.0,
        info=SimpleNamespace(order_price_round=0.1),
        settings=settings,
    )
    assert 98.7 <= price <= 100.0
    assert price == 99.4
    assert audit["model"] == "closed_5m_structure"
    assert audit["source"] == "ema20"

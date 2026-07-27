from types import SimpleNamespace
import time
from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.gate.normalizer import normalize_contract
from app.trading.risk import TradingRiskError, adaptive_operational_leverage, build_execution_plan, dynamic_position_notional, managed_stop_candidate, max_leverage_for_notional, notional_for_contract, partial_close_size, planned_take_profit_sizes
from app.trading.service import TradingService


def _info(name: str = "BTC_USDT"):
    return normalize_contract(
        {
            "name": name,
            "status": "trading",
            "type": "direct",
            "quanto_multiplier": "0.0001",
            "leverage_min": "1",
            "leverage_max": "100",
            "order_size_min": "1",
            "order_size_max": "1000000",
            "enable_decimal": False,
        }
    )


def _candidate():
    return {
        "contract": "BTC_USDT",
        "direction": "long",
        "market_state": "bullish",
        "ranking_score": 80,
        "risk_flags": [],
        "metrics": {
            "ticker": {"mark_price": 100000, "last": 100000},
            "15m": {"atr": 1000, "recent_low": 99000, "recent_high": 101000},
            "30m": {},
            "5m": {},
        },
    }


def test_execution_plan_has_profitable_rr_and_runner_split():
    settings = Settings(_env_file=None)
    plan = build_execution_plan(_candidate(), _info(), settings, 100000)
    assert plan["take_profits"][0]["rr"] >= 1
    assert [item["percent"] for item in plan["take_profits"]] == [0.30, 0.30, 0.25]
    assert plan["runner_percent"] == 0.15
    assert plan["initial_stop"] < plan["entry_price"]


def test_notional_uses_gate_contract_multiplier():
    size, actual_notional = notional_for_contract(_info(), 100000, 40000)
    assert size == "4000"
    assert actual_notional == 40000


def test_gate_market_slip_ratio_is_clamped_to_gate_limit():
    settings = Settings(_env_file=None, gate_market_order_slip_ratio=0.03)
    assert settings.gate_market_order_slip_ratio == 0.015


def test_margin_mode_is_forced_to_cross_even_if_environment_is_stale():
    settings = Settings(_env_file=None, gate_margin_mode="isolated")
    assert settings.gate_margin_mode == "cross"


def test_position_mode_is_forced_to_single_even_if_environment_is_stale():
    settings = Settings(_env_file=None, gate_position_mode="dual_plus")
    assert settings.gate_position_mode == "single"


def test_bitget_limit_entry_rounds_subunit_price_without_zeroing_it():
    info = SimpleNamespace(order_price_round=0.0001)
    price = TradingService._limit_entry_price(
        {"highest_bid": "0.1074", "lowest_ask": "0.1075"},
        "long",
        0.1075,
        info,
        0.0005,
    )
    assert price == 0.1073
    assert price > 0


@pytest.mark.asyncio
async def test_existing_single_position_does_not_call_global_mode_switch():
    class HeldSingleRest(FakeRest):
        async def get_positions(self, contract=None):
            return [{"contract": "LAB_USDT", "size": "1", "mode": "single"}]

        async def set_position_mode(self, position_mode):
            raise AssertionError("must not switch mode while a single position is held")

    service = TradingService(SimpleNamespace(rest=HeldSingleRest()), FakeRepository(), Settings(_env_file=None))
    await service._ensure_single_position_mode()


def test_stop_distance_uses_actual_loss_limit_instead_of_atr_distance_limit():
    settings = Settings(_env_file=None, max_initial_stop_loss_usdt=1000, maximum_stop_atr=40)
    candidate = _candidate()
    candidate["metrics"]["15m"] = {"atr": 100, "recent_low": 99000, "recent_high": 100100}
    plan = build_execution_plan(candidate, _info(), settings, 100000, risk_notional_usdt=40000)
    assert plan["initial_risk_distance"] > 3.5 * 100
    assert plan["estimated_stop_loss_usdt"] <= 1000


def test_stop_loss_over_actual_loss_limit_is_rejected():
    settings = Settings(_env_file=None, max_initial_stop_loss_usdt=1000, maximum_stop_atr=40)
    candidate = _candidate()
    candidate["metrics"]["15m"] = {"atr": 100, "recent_low": 97000, "recent_high": 100100}
    with pytest.raises(TradingRiskError) as error:
        build_execution_plan(candidate, _info(), settings, 100000, risk_notional_usdt=40000)
    assert error.value.code == "STOP_LOSS_OVER_LIMIT"


def test_partial_close_preserves_integer_contract_zeros():
    assert partial_close_size("long", "4000", 0.25) == "-1000"


def test_remaining_take_profits_keep_original_allocations_after_tp1():
    settings = Settings(_env_file=None)
    plan = build_execution_plan(_candidate(), _info(), settings, 100000)
    plan["initial_position_size"] = 4000
    plan["entry_size"] = 2800
    plan["completed_stages"] = ["TP1"]
    sizes = planned_take_profit_sizes(plan, 2800)
    assert sizes == {"TP2": "-1200", "TP3": "-1000"}
    assert 2800 - sum(abs(float(value)) for value in sizes.values()) == 600


def test_take_profit_sizes_follow_exchange_step_and_skip_subminimum_dust():
    settings = Settings(_env_file=None)
    plan = build_execution_plan(_candidate(), _info(), settings, 100000)
    plan["initial_position_size"] = 1.01
    plan["entry_size"] = 1.01
    plan["size_step"] = "0.1"
    plan["order_size_min"] = "0.2"
    sizes = planned_take_profit_sizes(plan, 1.01)
    assert sizes == {"TP1": "-0.3", "TP2": "-0.3", "TP3": "-0.2"}


def test_leverage_uses_the_tier_that_can_hold_target_notional():
    tiers = [
        {"risk_limit": "20000", "leverage_max": "100"},
        {"risk_limit": "50000", "leverage_max": "40"},
    ]
    assert max_leverage_for_notional(_info(), tiers, 40000) == 40


def test_operational_leverage_keeps_liquidation_buffer_beyond_stop():
    settings = Settings(_env_file=None)
    assert adaptive_operational_leverage(75, 100, 99, settings) == 20
    assert adaptive_operational_leverage(75, 100, 90, settings) == 5


def test_operational_leverage_floors_bank_style_decimal_to_exchange_integer():
    settings = Settings(_env_file=None)
    leverage = adaptive_operational_leverage(
        75,
        100,
        68.927,
        settings,
    )
    assert leverage == 1
    assert leverage.is_integer()


def test_operational_leverage_rejects_when_contract_minimum_exceeds_safe_cap():
    settings = Settings(_env_file=None)
    with pytest.raises(TradingRiskError) as error:
        adaptive_operational_leverage(
            75,
            100,
            68.927,
            settings,
            minimum_leverage=2,
        )
    assert error.value.code == "LEVERAGE_SAFETY_UNAVAILABLE"


def test_exchange_preflight_error_is_not_mislabeled_as_strategy_risk():
    action = TradingService._trading_error_action(
        "BANK_USDT",
        TradingRiskError(
            "CROSS_MARGIN_NOT_CONFIRMED",
            "cannot confirm cross margin",
        ),
    )
    assert action["status"] == "failed_exchange_preflight"
    assert action["code"] == "CROSS_MARGIN_NOT_CONFIRMED"


def test_dynamic_sizing_uses_equity_and_stop_distance():
    settings = Settings(_env_file=None)
    result = dynamic_position_notional(
        equity=10_000, available_margin=8_000, entry=100, stop=98,
        metrics=_candidate()["metrics"], side="long", stop_quality="STRUCTURE",
        market_state="normal", risk_flags=[],
        settings=settings, leverage=10,
    )
    assert result["notional"] > 4_000
    assert result["risk_budget_usdt"] <= 130


def test_test_mode_is_exactly_one_tenth_of_normal_dynamic_size():
    settings = Settings(_env_file=None)
    common = dict(
        equity=10_000, available_margin=8_000, entry=100, stop=98,
        metrics=_candidate()["metrics"], side="long", stop_quality="STRUCTURE",
        market_state="normal", risk_flags=[],
        settings=settings, leverage=10,
    )
    live = dynamic_position_notional(**common, mode="live")
    test = dynamic_position_notional(**common, mode="test")
    assert test["notional"] == pytest.approx(live["notional"] / 10)


def test_stop_is_placed_beyond_relevant_coinglass_liquidity_pool():
    settings = Settings(_env_file=None)
    candidate = _candidate()
    candidate["metrics"]["coinglass"] = {
        "heatmap": {
            "strongest_levels": [
                {"price": 97800, "usd": 2_000_000},
                {"price": 104000, "usd": 3_000_000},
            ]
        }
    }
    plan = build_execution_plan(candidate, _info(), settings, 100000)
    assert plan["initial_stop"] < 97800
    assert plan["stop_quality"] == "LIQUIDITY_ADJUSTED"


def test_remote_coinglass_pool_cannot_invent_a_wider_stop():
    settings = Settings(_env_file=None)
    candidate = _candidate()
    candidate["metrics"]["coinglass"] = {
        "heatmap": {"strongest_levels": [{"price": 96000, "usd": 5_000_000}]}
    }
    plan = build_execution_plan(candidate, _info(), settings, 100000)
    assert plan["initial_stop"] == pytest.approx(98100)
    assert plan["stop_quality"] == "STRUCTURE"
    assert "COINGLASS" not in plan["stop_source"]


def test_30m_thesis_invalidation_has_priority_when_tradable():
    settings = Settings(_env_file=None)
    candidate = _candidate()
    candidate["metrics"]["30m"] = {
        "atr": 1500,
        "recent_low": 98000,
        "recent_high": 104000,
    }
    plan = build_execution_plan(candidate, _info(), settings, 100000)
    assert plan["initial_stop"] == pytest.approx(97175)
    assert plan["stop_source"] == "30m_THESIS"
    assert plan["stop_components"]["15m_structure_stop"] == pytest.approx(98100)


def test_outlier_30m_invalidation_falls_back_to_15m_structure():
    settings = Settings(_env_file=None)
    candidate = _candidate()
    candidate["metrics"]["30m"] = {
        "atr": 3000,
        "recent_low": 80000,
        "recent_high": 104000,
    }
    plan = build_execution_plan(candidate, _info(), settings, 100000)
    assert plan["initial_stop"] == pytest.approx(98100)
    assert plan["stop_source"] == "15m_STRUCTURE"


def test_tighter_30m_print_cannot_override_wider_15m_invalidation():
    settings = Settings(_env_file=None)
    candidate = _candidate()
    candidate["metrics"]["30m"] = {
        "atr": 500,
        "recent_low": 99500,
        "recent_high": 104000,
    }
    plan = build_execution_plan(candidate, _info(), settings, 100000)
    assert plan["initial_stop"] == pytest.approx(98100)
    assert plan["stop_source"] == "15m_STRUCTURE_SUPERSEDES_TIGHTER_30m"


def test_take_profit_prefers_30m_structure_only_in_its_stage_band():
    settings = Settings(_env_file=None)
    candidate = _candidate()
    candidate["metrics"]["30m"] = {
        "atr": 1000,
        "recent_low": 99000,
        "recent_high": 105000,
    }
    plan = build_execution_plan(candidate, _info(), settings, 100000)
    assert plan["take_profits"][0]["source"] == "R_multiple"
    assert plan["take_profits"][1]["source"] == "30m_structure"
    assert plan["take_profits"][0]["price"] < plan["take_profits"][1]["price"] < plan["take_profits"][2]["price"]


def test_managed_stop_waits_for_proof_then_covers_fees():
    settings = Settings(_env_file=None)
    plan = {
        "side": "long",
        "current_stop": 98,
        "initial_risk_distance": 2,
        "atr15": 1,
        "market_state": "bullish",
        "completed_stages": [],
    }
    early = managed_stop_candidate(
        plan=plan, context={"atr15": 1}, price=102, entry=100, settings=settings
    )
    assert early["candidate_stop"] == 98
    assert early["phase"] == "INITIAL_RISK"

    plan.update(early)
    confirmed = managed_stop_candidate(
        plan=plan, context={"atr15": 1}, price=102.4, entry=100, settings=settings
    )
    assert confirmed["candidate_stop"] > 100
    assert confirmed["phase"] == "FEE_BREAK_EVEN"


def test_confirmed_expansion_widens_initial_risk_and_keeps_more_runner():
    settings = Settings(_env_file=None)
    candidate = _candidate()
    candidate["metrics"]["volatility_72h"] = {
        "available": True,
        "regime": "expansion",
        "effective_atr_pct": 0.02,
    }
    plan = build_execution_plan(candidate, _info(), settings, 100000)
    assert plan["planning_atr"] == pytest.approx(2000)
    assert plan["initial_risk_distance"] >= 1.6 * plan["planning_atr"]
    assert [item["percent"] for item in plan["take_profits"]] == [
        0.25,
        0.30,
        0.25,
    ]
    assert plan["runner_percent"] == 0.20


def test_expansion_does_not_move_to_break_even_on_ordinary_1_2r_noise():
    settings = Settings(_env_file=None)
    plan = {
        "side": "long",
        "current_stop": 98,
        "initial_risk_distance": 2,
        "atr15": 1,
        "market_state": "bullish",
        "completed_stages": [],
        "volatility_profile": {
            "available": True,
            "regime": "expansion",
            "effective_atr": 1,
        },
    }
    early = managed_stop_candidate(
        plan=plan,
        context={"atr15": 1},
        price=102.4,
        entry=100,
        settings=settings,
    )
    assert early["candidate_stop"] == 98
    assert early["break_even_activation_r"] == 1.5


def test_isolated_spike_uses_robust_scale_instead_of_short_atr_outlier():
    settings = Settings(_env_file=None)
    plan = {
        "side": "long",
        "current_stop": 98,
        "initial_risk_distance": 2,
        "atr15": 8,
        "market_state": "bullish",
        "completed_stages": [],
        "volatility_profile": {
            "available": True,
            "regime": "isolated_spike",
            "effective_atr": 1.2,
        },
    }
    trail = managed_stop_candidate(
        plan=plan,
        context={"atr15": 8},
        price=102.5,
        entry=100,
        settings=settings,
    )
    assert trail["management_atr"] == pytest.approx(1.2)
    assert trail["candidate_stop"] == 98
    assert trail["break_even_activation_r"] == 1.6


def test_runner_trail_uses_favorable_extreme_and_never_loosens_on_retrace():
    settings = Settings(_env_file=None)
    plan = {
        "side": "long",
        "current_stop": 100.2,
        "initial_risk_distance": 2,
        "atr15": 1,
        "market_state": "bullish",
        "completed_stages": ["TP1", "TP2"],
        "favorable_extreme": 107,
        "peak_r_multiple": 3.5,
    }
    trail = managed_stop_candidate(
        plan=plan,
        context={"atr15": 1, "recent_low15": 103},
        price=105,
        entry=100,
        settings=settings,
    )
    assert trail["favorable_extreme"] == 107
    assert trail["candidate_stop"] >= 103.8
    assert trail["phase"] == "RUNNER_TRAILING"


def test_stop_has_one_percent_noise_floor_when_atr_is_artificially_tiny():
    settings = Settings(_env_file=None)
    candidate = _candidate()
    candidate["metrics"]["15m"] = {
        "atr": 50,
        "recent_low": 99980,
        "recent_high": 100020,
    }
    plan = build_execution_plan(candidate, _info(), settings, 100000)
    assert plan["initial_risk_distance"] >= 1000
    assert plan["initial_stop"] <= 99000


def test_high_volatility_reduces_risk_without_creating_arbitrary_mini_size():
    settings = Settings(_env_file=None)
    common = dict(
        equity=10_000, available_margin=8_000, entry=100, stop=98,
        metrics=_candidate()["metrics"], side="long", stop_quality="STRUCTURE",
        risk_flags=[], settings=settings, leverage=10,
    )
    normal = dynamic_position_notional(**common, market_state="normal")
    volatile = dynamic_position_notional(**common, market_state="high_volatility")
    assert 0 < volatile["notional"] < normal["notional"]
    assert volatile["risk_pct_equity"] >= settings.min_risk_per_trade_pct


def test_small_risk_size_is_promoted_within_hard_risk_cap():
    settings = Settings(_env_file=None)
    result = dynamic_position_notional(
        equity=10_000, available_margin=8_000, entry=100, stop=92,
        metrics=_candidate()["metrics"], side="long", stop_quality="STRUCTURE",
        market_state="normal", risk_flags=[], settings=settings, leverage=2,
    )
    assert result["promoted_from_risk_size"] is True
    assert result["notional"] >= settings.minimum_viable_position_equity_multiple * 10_000
    assert result["actual_risk_pct_equity"] <= settings.max_promoted_risk_per_trade_pct


def test_execution_sizing_is_independent_of_scanner_ranking_score():
    settings = Settings(_env_file=None)
    candidate = _candidate()
    candidate["ranking_score"] = 99
    common = dict(
        equity=10_000, available_margin=8_000, entry=100, stop=98,
        metrics=candidate["metrics"], side="long", stop_quality="STRUCTURE",
        market_state="normal", risk_flags=[], settings=settings, leverage=10,
    )
    high_rank = dynamic_position_notional(**common)
    candidate["ranking_score"] = 56
    low_rank = dynamic_position_notional(**common)
    assert high_rank["notional"] == low_rank["notional"]


class FakeRepository:
    def __init__(self):
        self.positions = {}
        self.events = []
        self.paused = False

    async def get_trading_control(self):
        return {"paused": self.paused, "reason": None}

    async def set_trading_paused(self, paused, reason=None):
        self.paused = paused
        return {"paused": paused, "reason": reason}

    async def save_managed_position(self, value):
        self.positions[value["position_key"]] = value

    async def get_managed_position(self, key):
        return self.positions.get(key)

    async def list_managed_positions(self, active_only=False):
        values = list(self.positions.values())
        return [value for value in values if not active_only or value["status"] == "active"]

    async def save_order_event(self, value):
        self.events.append(value)


class FakeRest:
    def __init__(self):
        self.positions = []
        self.placed = []
        self.protection = []
        self.cancelled_entries = []
        self.cancelled_protection = set()

    async def get_positions(self, contract=None):
        return self.positions

    async def get_account(self):
        return {
            "in_dual_mode": False,
            "position_mode": "single",
            "total": "100000",
            "available": "90000",
        }

    async def set_position_mode(self, position_mode):
        self.position_mode = position_mode
        return {"in_dual_mode": False, "position_mode": position_mode}

    async def get_open_orders(self, contract=None, limit=100):
        return []

    async def get_contracts(self):
        return [
            {
                "name": "BTC_USDT",
                "status": "trading",
                "type": "direct",
                "quanto_multiplier": "0.0001",
                "leverage_max": "100",
                "order_size_min": "1",
                "order_size_max": "1000000",
                "enable_decimal": False,
            }
        ]

    async def get_ticker(self, contract):
        return {"contract": contract, "mark_price": "100000", "last": "100000"}

    async def set_leverage(self, contract, leverage, margin_mode):
        self.leverage = (contract, leverage, margin_mode)
        return {"contract": contract, "pos_margin_mode": margin_mode, "leverage": "0", "cross_leverage_limit": str(leverage)}

    async def set_position_margin_mode(self, contract, margin_mode):
        self.margin_mode = (contract, margin_mode)
        return {"contract": contract, "pos_margin_mode": margin_mode}

    async def set_cross_leverage_legacy(self, contract, leverage):
        self.legacy_margin_mode = (contract, leverage)
        return {"contract": contract, "pos_margin_mode": "cross", "leverage": "0", "cross_leverage_limit": str(leverage)}

    async def place_futures_order(self, body):
        self.placed.append(body)
        if body.get("reduce_only"):
            return {"id": "emergency"}
        self.positions = [
            {
                "contract": "BTC_USDT",
                "size": "4000",
                "entry_price": "100000",
                "lever": "100",
                "pos_margin_mode": body.get("pos_margin_mode", "cross"),
            }
        ]
        return {"id": "entry", "status": "finished", "finish_as": "filled"}

    async def get_position(self, contract):
        return self.positions[0] if self.positions else None

    async def create_price_order(self, body):
        self.protection.append(body)
        return {"id_string": str(len(self.protection))}

    async def get_price_orders(self, status="open", contract=None, limit=100):
        return [
            {"id_string": str(index)}
            for index in range(1, len(self.protection) + 1)
            if str(index) not in self.cancelled_protection
        ]

    async def cancel_futures_order(self, order_id):
        self.cancelled_entries.append(str(order_id))
        return {}

    async def cancel_price_order(self, order_id):
        self.cancelled_protection.add(str(order_id))
        return {}


@pytest.mark.asyncio
async def test_uncertain_entry_submission_is_recovered_by_client_oid():
    class UncertainEntryRest(FakeRest):
        async def place_futures_order(self, body):
            raise TimeoutError("response lost after exchange acceptance")

        async def get_order_detail(self, order_id=None, client_oid=None, contract=None):
            return {
                "id_string": "recovered-entry",
                "clientOid": client_oid,
                "state": "live",
                "contract": contract,
            }

    service = TradingService(
        SimpleNamespace(rest=UncertainEntryRest()),
        FakeRepository(),
        Settings(_env_file=None),
    )
    response = await service._submit_entry_with_recovery(
        contract="BTC_USDT",
        body={"contract": "BTC_USDT", "size": "1"},
        client_id="t-auto-entry-recover",
    )
    assert response["id"] == "recovered-entry"
    assert response["recovered_after_uncertain_submit"] is True


@pytest.mark.asyncio
async def test_uncertain_protection_submission_is_recovered_by_client_oid():
    class UncertainProtectionRest(FakeRest):
        def __init__(self):
            super().__init__()
            self.last_client_oid = None

        async def create_price_order(self, body):
            self.last_client_oid = body["initial"]["text"]
            raise TimeoutError("response lost after exchange acceptance")

        async def get_price_orders(
            self,
            status="open",
            contract=None,
            limit=100,
            client_oid=None,
        ):
            return [
                {
                    "id_string": "recovered-stop",
                    "clientOid": self.last_client_oid,
                    "text": self.last_client_oid,
                }
            ]

    rest = UncertainProtectionRest()
    settings = Settings(_env_file=None)
    service = TradingService(
        SimpleNamespace(rest=rest),
        FakeRepository(),
        settings,
    )
    plan = build_execution_plan(_candidate(), _info(), settings, 100000)
    order_id = await service._create_trigger(
        plan,
        "stop",
        plan["initial_stop"],
        "4000",
        "BTC_USDT:long",
    )
    assert order_id == "recovered-stop"


@pytest.mark.asyncio
async def test_exchange_max_open_preflight_caps_entry_before_submission():
    class CapacityRest(FakeRest):
        async def get_max_openable_quantity(self, contract, side, price):
            return 3000

        async def place_futures_order(self, body):
            self.placed.append(body)
            if body.get("reduce_only"):
                return {"id": "emergency"}
            self.positions = [
                {
                    "contract": body["contract"],
                    "size": str(abs(float(body["size"]))),
                    "entry_price": body["price"],
                    "lever": str(self.leverage[1]),
                    "pos_margin_mode": body.get("pos_margin_mode", "cross"),
                }
            ]
            return {"id": "entry", "status": "finished", "finish_as": "filled"}

    rest = CapacityRest()
    service = TradingService(
        SimpleNamespace(rest=rest),
        FakeRepository(),
        Settings(_env_file=None, auto_order_enabled=True),
    )
    result = await service.process_scan(
        {"rankings": {"combined": [_candidate()]}}
    )
    action = result["orders"][0]
    entry = next(item for item in rest.placed if not item.get("reduce_only"))
    assert action["status"] == "submitted"
    assert abs(float(entry["size"])) == 3000
    assert action["position_sizing"]["exchange_capacity_capped"] is True


@pytest.mark.asyncio
async def test_exchange_capacity_cannot_force_an_ant_sized_live_position():
    class TinyCapacityRest(FakeRest):
        async def get_max_openable_quantity(self, contract, side, price):
            return 1000

    rest = TinyCapacityRest()
    service = TradingService(
        SimpleNamespace(rest=rest),
        FakeRepository(),
        Settings(_env_file=None, auto_order_enabled=True),
    )
    result = await service.process_scan(
        {"rankings": {"combined": [_candidate()]}}
    )
    action = result["orders"][0]
    assert action["status"] == "skipped_exchange_constraint"
    assert action["code"] == "EXCHANGE_CAPACITY_BELOW_VIABLE_POSITION"
    assert rest.placed == []


@pytest.mark.asyncio
async def test_repeated_scan_does_not_open_same_contract_twice():
    settings = Settings(_env_file=None, auto_order_enabled=True, position_management_enabled=False)
    rest = FakeRest()
    repo = FakeRepository()
    notifications = []

    class Notifier:
        async def send_order(self, action):
            notifications.append(action)

    service = TradingService(SimpleNamespace(rest=rest), repo, settings, Notifier())
    first = await service.process_scan({"rankings": {"combined": [_candidate()]}})
    second = await service.process_scan({"rankings": {"combined": [_candidate()]}})
    assert first["orders"][0]["status"] == "submitted"
    assert first["orders"][0]["protection_status"] == "exchange"
    assert second["orders"][0]["status"] == "skipped_existing_position"
    assert len([item for item in rest.placed if not item.get("reduce_only")]) == 1
    assert len(rest.protection) == 4
    assert len(notifications) == 2
    assert notifications[1]["status"] == "skipped_existing_position"
    assert rest.margin_mode == ("BTC_USDT", "cross")
    assert rest.leverage[2] == "cross"
    entry = next(item for item in rest.placed if not item.get("reduce_only"))
    assert entry["tif"] == "gtc"
    assert entry["price"] != "0"
    assert entry["pos_margin_mode"] == "cross"
    assert entry["tpsl_sl_trigger_price"] != "0"
    assert "tpsl_tp_trigger_price" not in entry
    assert "market_order_slip_ratio" not in entry
    assert all(item["pos_margin_mode"] == "cross" for item in rest.protection)
    assert rest.protection[0]["order_type"] == "close-long-position"
    assert [item["order_type"] for item in rest.protection[1:]] == [
        "plan-close-long-position",
        "plan-close-long-position",
        "plan-close-long-position",
    ]
    assert rest.protection[0]["trigger"]["price_type"] == 1
    assert all(item["trigger"]["price_type"] == 0 for item in rest.protection[1:])
    assert all(isinstance(item["initial"]["size"], int) for item in rest.protection[1:])


@pytest.mark.asyncio
async def test_same_direction_scan_refreshes_position_time_stop():
    settings = Settings(_env_file=None, auto_order_enabled=True)
    rest = FakeRest()
    repo = FakeRepository()
    service = TradingService(SimpleNamespace(rest=rest), repo, settings)
    await service.process_scan({"rankings": {"combined": [_candidate()]}})
    before = await repo.get_managed_position("BTC_USDT:long")
    old_deadline = before["plan"]["time_stop_deadline"]
    old_confirmations = before["plan"]["trend_confirmations"]
    await service.process_scan({"rankings": {"combined": [_candidate()]}})
    after = await repo.get_managed_position("BTC_USDT:long")
    assert after["plan"]["trend_confirmations"] == old_confirmations + 1
    assert after["plan"]["time_stop_deadline"] >= old_deadline


@pytest.mark.asyncio
async def test_time_decay_closes_stagnant_position_after_regime_deadline():
    settings = Settings(_env_file=None, position_management_enabled=True)
    rest = FakeRest()
    repo = FakeRepository()
    service = TradingService(SimpleNamespace(rest=rest), repo, settings)
    plan = build_execution_plan(_candidate(), _info(), settings, 100000)
    record = service._managed_payload("BTC_USDT:long", plan, 4000, {}, 100)
    plan["opened_at"] = "2026-01-01T00:00:00+00:00"
    plan["time_stop_deadline"] = "2026-01-01T12:00:00+00:00"
    closed = []

    async def fake_close(contract, position):
        closed.append(contract)

    async def no_missing(plan_value, size, key):
        return set()

    service._close_for_trend_break = fake_close
    service._ensure_protection = no_missing
    result = await service._manage_position(
        record,
        {"contract": "BTC_USDT", "size": "4000", "entry_price": "100000", "pos_margin_mode": "cross"},
        {"mark_price": "100000", "last": "100000"},
        {"atr15": 1000, "atr5": 500},
        _info(),
    )
    assert result["status"] == "time_decay_closed"
    assert closed == ["BTC_USDT"]


@pytest.mark.asyncio
async def test_scan_refresh_cannot_tighten_initial_stop_immediately_after_fill():
    settings = Settings(_env_file=None, auto_order_enabled=True)
    rest = FakeRest()
    repo = FakeRepository()
    service = TradingService(SimpleNamespace(rest=rest), repo, settings)
    first = await service.process_scan({"rankings": {"combined": [_candidate()]}})
    assert first["orders"][0]["status"] == "submitted"

    refreshed = _candidate()
    refreshed["metrics"]["15m"] = {
        "atr": 1000,
        "recent_low": 99800,
        "recent_high": 101000,
    }
    second = await service.process_scan({"rankings": {"combined": [refreshed]}})
    update = second["position_updates"][0]
    assert update["status"] == "unchanged"
    assert "1" not in rest.cancelled_protection
    record = await repo.get_managed_position("BTC_USDT:long")
    assert record["plan"]["protection_order_ids"]["stop"] == "1"
    assert record["plan"]["latest_scan_invalidation"] > 0


@pytest.mark.asyncio
async def test_scan_recalculates_held_coin_that_fell_out_of_qualified_rankings():
    settings = Settings(_env_file=None, auto_order_enabled=True)
    rest = FakeRest()
    repo = FakeRepository()
    service = TradingService(SimpleNamespace(rest=rest), repo, settings)
    first = await service.process_scan({"rankings": {"combined": [_candidate()]}})
    assert first["orders"][0]["status"] == "submitted"

    refreshed = _candidate()
    refreshed["ranking_score"] = 40
    refreshed["metrics"]["15m"] = {
        "atr": 1000,
        "recent_low": 99800,
        "recent_high": 101000,
    }
    second = await service.process_scan(
        {
            "rankings": {"combined": []},
            "_scan_analysis": {"BTC_USDT": refreshed},
        }
    )
    assert second["position_updates"][0]["scan_source"] == "scan_analysis_not_qualified"
    assert second["position_updates"][0]["scan_recalculated"] is True
    record = await repo.get_managed_position("BTC_USDT:long")
    assert record["plan"]["scan_signal_status"] == "same_direction_not_qualified"
    assert record["plan"]["scan_missing_count"] == 1


@pytest.mark.asyncio
async def test_scan_tp_does_not_chase_unfilled_target_farther_away():
    settings = Settings(_env_file=None)
    rest = FakeRest()
    service = TradingService(SimpleNamespace(rest=rest), FakeRepository(), settings)
    original = _candidate()
    original["metrics"]["15m"] = {
        "atr": 1000,
        "recent_low": 99000,
        "recent_high": 101000,
    }
    plan = build_execution_plan(original, _info(), settings, 100000, risk_notional_usdt=40000)
    await service._install_protection(plan, 4000, "BTC_USDT:long")
    old_tp2 = plan["take_profits"][1]["price"]
    record = {
        "position_key": "BTC_USDT:long",
        "current_size": 4000,
        "plan": plan,
    }

    wider = _candidate()
    wider["metrics"]["15m"] = {
        "atr": 1200,
        "recent_low": 99000,
        "recent_high": 101000,
    }
    proposed = build_execution_plan(wider, _info(), settings, 100000, risk_notional_usdt=40000)
    result = await service._apply_scan_protection_update(
        record,
        {"entry_price": "100000", "size": "4000"},
        {"mark_price": "102500", "last": "102500"},
        proposed,
        _info(),
    )
    assert result["changed"] == []
    assert plan["take_profits"][1]["price"] == old_tp2


@pytest.mark.asyncio
async def test_opposite_signal_must_persist_before_position_reversal():
    class ReverseRest(FakeRest):
        def __init__(self):
            super().__init__()
            self.positions = [{"contract": "BTC_USDT", "size": "-4000", "entry_price": "100000", "mode": "single"}]
            self.events = []

        async def get_positions(self, contract=None):
            return [item for item in self.positions if contract is None or item["contract"] == contract]

        async def get_open_orders(self, contract=None, limit=100):
            return []

        async def cancel_all_price_orders(self, contract=None):
            self.events.append("cancel_protection")
            return {"successList": []}

        async def place_futures_order(self, body):
            self.events.append("close" if body.get("reduce_only") else "entry")
            if body.get("reduce_only"):
                self.positions = []
                return {"id": "reverse-close"}
            return {"id": "reverse-entry"}

    settings = Settings(_env_file=None, auto_order_enabled=True)
    rest = ReverseRest()
    repo = FakeRepository()
    repo.positions["BTC_USDT:short"] = {
        "position_key": "BTC_USDT:short",
        "contract": "BTC_USDT",
        "side": "short",
        "status": "active",
        "plan": {"contract": "BTC_USDT", "side": "short"},
    }
    service = TradingService(SimpleNamespace(rest=rest), repo, settings)

    async def fake_open(candidate, info):
        return {"contract": info.name, "status": "limit_order_open", "side": candidate["direction"], "size": 1}

    service._open_candidate = fake_open
    candidate = _candidate()
    candidate["metrics"]["30m"]["closed_timestamp"] = "2026-07-27T12:30:00+00:00"
    first = await service._process_candidates([candidate])
    assert first["orders"][0]["status"] == "reversal_confirmation_pending"
    assert rest.events == []

    candidate["metrics"]["30m"]["closed_timestamp"] = "2026-07-27T13:00:00+00:00"
    result = await service._process_candidates([candidate])
    action = result["orders"][0]
    assert action["status"] == "limit_order_open"
    assert action["reversed"] is True
    assert action["reversal_from"] == "short"
    assert rest.events == ["cancel_protection", "close"]


@pytest.mark.asyncio
async def test_backend_fallback_closes_reduce_only_when_exchange_stop_cannot_be_installed():
    class BrokenProtectionRest(FakeRest):
        async def create_price_order(self, body):
            raise RuntimeError("Gate protection unavailable")

        async def get_ticker(self, contract):
            return {"contract": contract, "mark_price": "98000", "last": "98000"}

    settings = Settings(_env_file=None, position_management_enabled=True)
    rest = BrokenProtectionRest()
    rest.positions = [{"contract": "BTC_USDT", "size": "4000", "entry_price": "100000", "lever": "100"}]
    repo = FakeRepository()
    service = TradingService(SimpleNamespace(rest=rest), repo, settings)
    plan = build_execution_plan(_candidate(), _info(), settings, 100000)
    record = service._managed_payload("BTC_USDT:long", plan, 4000, {}, 100)
    record["protection_status"] = "backend_fallback"
    result = await service._manage_position(record, rest.positions[0], {"mark_price": "98000"}, {}, _info())
    assert result["fallback_actions"][0]["status"] == "fallback_stop_submitted"
    assert rest.placed[-1]["reduce_only"] is True
    assert rest.placed[-1]["close"] is True


@pytest.mark.asyncio
async def test_pending_limit_order_is_cancelled_when_momentum_moves_away():
    class PendingRest(FakeRest):
        cancelled = False

        async def get_open_orders(self, contract=None, limit=100):
            if self.cancelled:
                return []
            return [
                {
                    "id": "pending-1",
                    "text": "t-auto-entry-test",
                    "contract": "BTC_USDT",
                    "size": "4000",
                    "price": "100000",
                    "create_time": time.time(),
                }
            ]

        async def cancel_futures_order(self, order_id):
            self.cancelled = True
            self.cancelled_entries.append(str(order_id))
            return {"orderId": order_id}

        async def get_positions(self, contract=None):
            return []

        async def get_ticker(self, contract):
            return {"contract": contract, "mark_price": "101600", "last": "101600"}

    settings = Settings(
        _env_file=None,
        auto_order_enabled=True,
        limit_entry_min_observation_seconds=0,
        limit_entry_momentum_confirmations=1,
    )
    rest = PendingRest()
    service = TradingService(SimpleNamespace(rest=rest), FakeRepository(), settings)
    actions = await service._monitor_pending_entries()
    assert actions[0]["code"] == "LIMIT_ENTRY_MOMENTUM"
    assert rest.cancelled_entries == ["pending-1"]


@pytest.mark.asyncio
async def test_pending_limit_order_ignores_first_normal_volatility_tick():
    class VolatileRest(FakeRest):
        cancelled = False

        async def get_open_orders(self, contract=None, limit=100):
            if self.cancelled:
                return []
            return [{
                "id": "volatile-1",
                "text": "t-auto-entry-volatile",
                "contract": "BTC_USDT",
                "size": "4000",
                "price": "100000",
                "create_time": time.time(),
            }]

        async def cancel_futures_order(self, order_id):
            self.cancelled = True
            self.cancelled_entries.append(str(order_id))
            return {"orderId": order_id}

        async def get_positions(self, contract=None):
            return []

        async def get_ticker(self, contract):
            return {"contract": contract, "mark_price": "101000", "last": "101000"}

    settings = Settings(_env_file=None, auto_order_enabled=True)
    rest = VolatileRest()
    service = TradingService(SimpleNamespace(rest=rest), FakeRepository(), settings)
    actions = await service._monitor_pending_entries()
    assert actions == []
    assert rest.cancelled_entries == []


@pytest.mark.asyncio
async def test_pending_limit_order_requires_consecutive_momentum_confirmations():
    class ConfirmRest(FakeRest):
        cancelled = False

        async def get_open_orders(self, contract=None, limit=100):
            if self.cancelled:
                return []
            return [{
                "id": "confirm-1",
                "text": "t-auto-entry-confirm",
                "contract": "BTC_USDT",
                "size": "4000",
                "price": "100000",
                "create_time": time.time() - 31,
            }]

        async def cancel_futures_order(self, order_id):
            self.cancelled = True
            self.cancelled_entries.append(str(order_id))
            return {"orderId": order_id}

        async def get_positions(self, contract=None):
            return []

        async def get_ticker(self, contract):
            return {"contract": contract, "mark_price": "101600", "last": "101600"}

    settings = Settings(_env_file=None, auto_order_enabled=True)
    rest = ConfirmRest()
    service = TradingService(SimpleNamespace(rest=rest), FakeRepository(), settings)
    first = await service._monitor_pending_entries()
    assert first == []
    second = await service._monitor_pending_entries()
    assert second[0]["code"] == "LIMIT_ENTRY_MOMENTUM"
    assert rest.cancelled_entries == ["confirm-1"]


@pytest.mark.asyncio
async def test_pending_limit_order_is_cleaned_after_three_hours():
    class StalePendingRest(FakeRest):
        cancelled = False

        async def get_open_orders(self, contract=None, limit=100):
            if self.cancelled:
                return []
            return [{
                "id": "stale-1",
                "text": "t-auto-entry-stale",
                "contract": "BTC_USDT",
                "size": "4000",
                "price": "100000",
                "create_time": time.time() - 10_801,
            }]

        async def cancel_futures_order(self, order_id):
            self.cancelled = True
            self.cancelled_entries.append(str(order_id))
            return {"orderId": order_id}

        async def get_positions(self, contract=None):
            return []

    settings = Settings(_env_file=None, limit_entry_timeout_seconds=10_800)
    rest = StalePendingRest()
    service = TradingService(SimpleNamespace(rest=rest), FakeRepository(), settings)
    actions = await service._monitor_pending_entries()
    assert actions[0]["code"] == "LIMIT_ENTRY_TIMEOUT"
    assert actions[0]["age_seconds"] >= 10_801
    assert rest.cancelled_entries == ["stale-1"]


@pytest.mark.asyncio
async def test_same_direction_batch_is_limited_by_portfolio_risk_not_order_count():
    class BatchRest(FakeRest):
        async def get_contracts(self):
            return [
                {
                    "name": name,
                    "status": "trading",
                    "type": "direct",
                    "quanto_multiplier": "0.0001",
                    "leverage_max": "100",
                    "order_size_min": "1",
                    "order_size_max": "1000000",
                    "enable_decimal": False,
                }
                for name in ("BTC_USDT", "ETH_USDT", "XRP_USDT")
            ]

    settings = Settings(_env_file=None, auto_order_enabled=True)
    service = TradingService(SimpleNamespace(rest=BatchRest()), FakeRepository(), settings)

    async def fake_open(candidate, info):
        return {"contract": info.name, "status": "limit_order_open", "side": candidate["direction"], "size": 1}

    service._open_candidate = fake_open
    result = await service._process_candidates(
        [
            {"contract": "BTC_USDT", "direction": "long"},
            {"contract": "ETH_USDT", "direction": "long"},
            {"contract": "XRP_USDT", "direction": "long"},
        ]
    )
    assert [item["status"] for item in result["orders"]] == [
        "limit_order_open",
        "limit_order_open",
        "limit_order_open",
    ]


@pytest.mark.asyncio
async def test_recent_closed_position_enters_reentry_cooldown():
    settings = Settings(_env_file=None, auto_order_enabled=True)
    repo = FakeRepository()
    repo.positions["BTC_USDT:long"] = {
        "position_key": "BTC_USDT:long",
        "contract": "BTC_USDT",
        "side": "long",
        "status": "closed",
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }
    service = TradingService(SimpleNamespace(rest=FakeRest()), repo, settings)
    result = await service.process_scan({"rankings": {"combined": [_candidate()]}})
    assert result["orders"][0]["status"] == "skipped_reentry_cooldown"
    assert result["orders"][0]["code"] == "REENTRY_COOLDOWN"


@pytest.mark.asyncio
async def test_pause_blocks_new_orders_but_is_a_persisted_control():
    settings = Settings(_env_file=None, auto_order_enabled=True)
    rest = FakeRest()
    repo = FakeRepository()
    service = TradingService(SimpleNamespace(rest=rest), repo, settings)
    await service.pause("operator requested")
    result = await service.process_scan({"rankings": {"combined": [_candidate()]}})
    assert result["status"] == "paused"
    assert rest.placed == []


@pytest.mark.asyncio
async def test_take_profit_extension_moves_only_in_favour_and_creates_replacement():
    settings = Settings(_env_file=None, auto_order_enabled=True)
    rest = FakeRest()
    service = TradingService(SimpleNamespace(rest=rest), FakeRepository(), settings)
    plan = build_execution_plan(_candidate(), _info(), settings, 100000)
    plan["current_r_multiple"] = 2.5
    plan["protection_order_ids"]["TP3"] = "old-tp3"
    record = {"position_key": "BTC_USDT:long", "current_size": 4}
    changed = await service._maybe_extend_take_profit(record, plan, 105500, {"atr15": 1000})
    assert changed is True
    assert plan["take_profits"][2]["price"] > 105700
    assert float(rest.protection[-1]["trigger"]["price"]) == plan["take_profits"][2]["price"]


@pytest.mark.asyncio
async def test_filled_exchange_tp_is_not_recreated_on_next_reconciliation():
    settings = Settings(_env_file=None, position_management_enabled=True)
    rest = FakeRest()
    service = TradingService(SimpleNamespace(rest=rest), FakeRepository(), settings)
    plan = build_execution_plan(_candidate(), _info(), settings, 100000)
    await service._install_protection(plan, 4000, "BTC_USDT:long")
    # FakeRest assigns stop=1, TP1=2, TP2=3, TP3=4.  A 30% reduction and a
    # missing TP1 trigger means Bitget already executed that partial target.
    rest.cancelled_protection.add("2")
    before = len(rest.protection)
    repaired = await service._ensure_protection(plan, 2800, "BTC_USDT:long")
    assert repaired == set()
    assert "TP1" in plan["completed_stages"]
    assert len(rest.protection) == before


@pytest.mark.asyncio
async def test_manual_exchange_protection_is_adopted_before_reconciliation():
    class ManualProtectionRest(FakeRest):
        async def get_price_orders(self, status="open", contract=None, limit=100):
            return [
                {
                    "id_string": "manual-sl",
                    "plan_type": "loss_plan",
                    "trigger": {"price": "98000", "price_type": 1},
                    "initial": {"size": "4000"},
                    "raw": {"planType": "loss_plan"},
                },
                {
                    "id_string": "manual-tp",
                    "plan_type": "profit_plan",
                    "trigger": {"price": "102000", "price_type": 0},
                    "initial": {"size": "1000"},
                    "raw": {"planType": "profit_plan"},
                },
            ]

    settings = Settings(_env_file=None)
    rest = ManualProtectionRest()
    service = TradingService(SimpleNamespace(rest=rest), FakeRepository(), settings)
    plan = build_execution_plan(_candidate(), _info(), settings, 100000)
    await service._seed_existing_protection_ids(plan, "BTC_USDT")
    assert plan["protection_order_ids"]["stop"] == "manual-sl"
    assert plan["protection_order_ids"]["TP1"] == "manual-tp"
    assert plan["protection_order_ids"]["TP2"] is None


@pytest.mark.asyncio
async def test_verified_managed_stop_removes_redundant_entry_preset():
    class PresetRest(FakeRest):
        async def get_price_orders(self, status="open", contract=None, limit=100):
            return [
                {
                    "id_string": "managed-stop",
                    "plan_type": "loss_plan",
                    "initial": {"size": "4000"},
                    "trigger": {"price": "99000"},
                    "text": "t-auto-sl-managed",
                },
                {
                    "id_string": "entry-preset",
                    "plan_type": "loss_plan",
                    "initial": {"size": "4000"},
                    "trigger": {"price": "99000"},
                    "text": "t-auto-entry-original",
                },
            ]

    rest = PresetRest()
    service = TradingService(SimpleNamespace(rest=rest), FakeRepository(), Settings(_env_file=None))
    plan = build_execution_plan(_candidate(), _info(), service.settings, 100000)
    await service._remove_redundant_entry_presets(plan, 4000, {"managed-stop"})
    assert "entry-preset" in rest.cancelled_protection
    assert "managed-stop" not in rest.cancelled_protection


def test_protection_validation_rejects_wrong_exchange_trigger():
    settings = Settings(_env_file=None)
    service = TradingService(SimpleNamespace(rest=FakeRest()), FakeRepository(), settings)
    plan = build_execution_plan(_candidate(), _info(), settings, 100000)
    assert service._protection_order_matches(
        plan,
        "stop",
        plan["current_stop"],
        "4000",
        {
            "plan_type": "loss_plan",
            "trigger": {"price": "99000", "price_type": 1},
            "initial": {"size": "4000"},
            "margin_mode": "cross",
            "mode": "single",
        },
    ) is False


def test_fast_timeframes_cannot_overturn_the_30m_entry_thesis():
    plan = {"side": "long"}
    context = {
        "last_close15": 90,
        "ema2015": 95,
        "ema5015": 100,
        "last_close5": 89,
        "ema205": 92,
        "recent_low15": 91,
        "recent_low5": 90,
    }
    assessment = TradingService._trend_break_assessment(plan, context)
    assert assessment["score"] == 3
    assert assessment["candidate"] is False
    assert assessment["evidence"] == [
        "15m_structure",
        "15m_ema",
        "5m_warning_only",
    ]


def test_closed_30m_structure_failure_can_exit_without_a_time_lock():
    plan = {"side": "long"}
    context = {
        "closed_timestamp30": "2026-07-27T12:30:00+00:00",
        "last_close30": 90,
        "recent_low30": 91,
        "last_close15": 89,
        "recent_low15": 90,
    }
    assessment = TradingService._trend_break_assessment(plan, context)
    confirmation = TradingService._update_thesis_failure_confirmation(
        plan, assessment, 2
    )
    assert assessment["failure_kind"] == "hard_structure"
    assert confirmation["confirmed"] is True


def test_soft_rollover_counts_completed_30m_candles_not_manager_ticks():
    plan = {"side": "long"}
    context = {
        "closed_timestamp30": "2026-07-27T12:30:00+00:00",
        "last_close30": 90,
        "ema2030": 95,
        "ema5030": 100,
        "last_close15": 89,
        "recent_low15": 90,
        "ema2015": 94,
        "ema5015": 99,
    }
    assessment = TradingService._trend_break_assessment(plan, context)
    first = TradingService._update_thesis_failure_confirmation(plan, assessment, 2)
    duplicate = TradingService._update_thesis_failure_confirmation(
        plan, assessment, 2
    )
    context["closed_timestamp30"] = "2026-07-27T13:00:00+00:00"
    second_assessment = TradingService._trend_break_assessment(plan, context)
    second = TradingService._update_thesis_failure_confirmation(
        plan, second_assessment, 2
    )
    assert assessment["failure_kind"] == "soft_rollover"
    assert first["confirmations"] == duplicate["confirmations"] == 1
    assert first["confirmed"] is duplicate["confirmed"] is False
    assert second["confirmed"] is True


def test_time_decay_horizon_adapts_to_coin_volatility_regime():
    settings = Settings(_env_file=None)
    normal = TradingService._time_stop_horizon_hours(
        {"market_state": "bullish"}, settings
    )
    expansion = TradingService._time_stop_horizon_hours(
        {
            "market_state": "bullish",
            "volatility_profile": {"regime": "expansion"},
        },
        settings,
    )
    compression = TradingService._time_stop_horizon_hours(
        {
            "market_state": "bullish",
            "volatility_profile": {"regime": "compression"},
        },
        settings,
    )
    assert expansion > normal > compression

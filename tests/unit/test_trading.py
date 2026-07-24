from types import SimpleNamespace
import time

import pytest

from app.config import Settings
from app.gate.normalizer import normalize_contract
from app.trading.risk import TradingRiskError, build_execution_plan, max_leverage_for_notional, notional_for_contract, partial_close_size
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


def test_execution_plan_has_rr_one_and_fixed_split():
    settings = Settings(_env_file=None)
    plan = build_execution_plan(_candidate(), _info(), settings, 100000)
    assert plan["take_profits"][0]["rr"] >= 1
    assert [item["percent"] for item in plan["take_profits"]] == [0.25, 0.30, 0.25]
    assert plan["runner_percent"] == 0.20
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
    settings = Settings(_env_file=None)
    candidate = _candidate()
    candidate["metrics"]["15m"] = {"atr": 100, "recent_low": 99000, "recent_high": 100100}
    plan = build_execution_plan(candidate, _info(), settings, 100000, risk_notional_usdt=40000)
    assert plan["initial_risk_distance"] > 3.5 * 100
    assert plan["estimated_stop_loss_usdt"] <= 1000


def test_stop_loss_over_actual_loss_limit_is_rejected():
    settings = Settings(_env_file=None)
    candidate = _candidate()
    candidate["metrics"]["15m"] = {"atr": 100, "recent_low": 97000, "recent_high": 100100}
    with pytest.raises(TradingRiskError) as error:
        build_execution_plan(candidate, _info(), settings, 100000, risk_notional_usdt=40000)
    assert error.value.code == "STOP_LOSS_OVER_LIMIT"


def test_partial_close_preserves_integer_contract_zeros():
    assert partial_close_size("long", "4000", 0.25) == "-1000"


def test_leverage_uses_the_tier_that_can_hold_target_notional():
    tiers = [
        {"risk_limit": "20000", "leverage_max": "100"},
        {"risk_limit": "50000", "leverage_max": "40"},
    ]
    assert max_leverage_for_notional(_info(), tiers, 40000) == 40


def test_zec_uses_ten_thousand_usdt_notional_without_becoming_a_driver_slot():
    settings = Settings(_env_file=None)
    service = TradingService(SimpleNamespace(rest=None), FakeRepository(), settings)
    assert service._notional("ZEC_USDT") == 10_000
    assert "ZEC_USDT" not in service._market_driver_contracts()


def test_stock_contracts_use_stock_notional_without_changing_crypto_defaults():
    settings = Settings(_env_file=None)
    service = TradingService(SimpleNamespace(rest=None), FakeRepository(), settings)
    assert service._notional("AMZNX_USDT", "stocks") == 5_000
    assert service._notional("BTC_USDT") == 20_000


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

    async def get_positions(self, contract=None):
        return self.positions

    async def get_account(self):
        return {"in_dual_mode": False, "position_mode": "single"}

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
        return [{"id_string": str(index)} for index in range(1, len(self.protection) + 1)]

    async def cancel_futures_order(self, order_id):
        self.cancelled_entries.append(str(order_id))
        return {}

    async def cancel_price_order(self, order_id):
        return {}


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
    assert len(notifications) == 1
    assert rest.margin_mode == ("BTC_USDT", "cross")
    assert rest.leverage[2] == "cross"
    entry = next(item for item in rest.placed if not item.get("reduce_only"))
    assert entry["tif"] == "gtc"
    assert entry["price"] != "0"
    assert entry["pos_margin_mode"] == "cross"
    assert entry["tpsl_sl_trigger_price"] != "0"
    assert "market_order_slip_ratio" not in entry
    assert all(item["pos_margin_mode"] == "cross" for item in rest.protection)
    assert rest.protection[0]["order_type"] == "close-long-position"
    assert [item["order_type"] for item in rest.protection[1:]] == [
        "plan-close-long-position",
        "plan-close-long-position",
        "plan-close-long-position",
    ]
    assert all(item["trigger"]["price_type"] == 0 for item in rest.protection)
    assert all(isinstance(item["initial"]["size"], int) for item in rest.protection[1:])


@pytest.mark.asyncio
async def test_opposite_signal_closes_position_before_opening_new_direction():
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
    service = TradingService(SimpleNamespace(rest=rest), FakeRepository(), settings)

    async def fake_open(candidate, info):
        return {"contract": info.name, "status": "limit_order_open", "side": candidate["direction"], "size": 1}

    service._open_candidate = fake_open
    result = await service._process_candidates([_candidate()])
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
        async def get_open_orders(self, contract=None, limit=100):
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

        async def get_positions(self, contract=None):
            return []

        async def get_ticker(self, contract):
            return {"contract": contract, "mark_price": "101000", "last": "101000"}

    settings = Settings(_env_file=None, auto_order_enabled=True)
    rest = PendingRest()
    service = TradingService(SimpleNamespace(rest=rest), FakeRepository(), settings)
    actions = await service._monitor_pending_entries()
    assert actions[0]["code"] == "LIMIT_ENTRY_MOMENTUM"
    assert rest.cancelled_entries == ["pending-1"]


@pytest.mark.asyncio
async def test_pending_limit_order_is_cleaned_after_three_hours():
    class StalePendingRest(FakeRest):
        async def get_open_orders(self, contract=None, limit=100):
            return [{
                "id": "stale-1",
                "text": "t-auto-entry-stale",
                "contract": "BTC_USDT",
                "size": "4000",
                "price": "100000",
                "create_time": time.time() - 10_801,
            }]

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
async def test_each_scan_batch_allows_at_most_two_same_direction_orders():
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
                for name in ("BTC_USDT", "ETH_USDT", "SOL_USDT")
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
            {"contract": "SOL_USDT", "direction": "long"},
        ]
    )
    assert [item["status"] for item in result["orders"]] == [
        "limit_order_open",
        "limit_order_open",
        "skipped_batch_direction_limit",
    ]


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

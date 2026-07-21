import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from app.gate.normalizer import closed_candles, normalize_candles
from app.indicators.atr import atr
from app.trading.risk import (
    TradingRiskError,
    build_execution_plan,
    max_leverage_for_notional,
    notional_for_contract,
    partial_close_size,
    signed_size,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _order_id(payload: dict[str, Any]) -> str | None:
    value = payload.get("id_string") or payload.get("id")
    return str(value) if value not in (None, "") else None


class TradingService:
    """Connect scanner results to Gate orders and keep protective orders alive.

    The scanner remains the source of entry candidates. This service only acts
    on qualified scanner results and never invents a symbol or direction.
    """

    def __init__(self, gate: Any, repository: Any, settings: Any, notifier: Any | None = None):
        self.gate = gate
        self.repository = repository
        self.settings = settings
        self.notifier = notifier
        self._order_lock = asyncio.Lock()
        self._manager_task: asyncio.Task | None = None
        self._running = False
        self._contract_cache: dict[str, Any] = {}
        self._contract_cache_at = 0.0
        self._market_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.auto_order_enabled)

    async def start(self) -> None:
        if not (self.settings.position_management_enabled or self.settings.auto_order_enabled) or self._manager_task is not None:
            return
        self._running = True
        self._manager_task = asyncio.create_task(self._management_loop())

    async def stop(self) -> None:
        self._running = False
        if self._manager_task:
            self._manager_task.cancel()
            try:
                await self._manager_task
            except asyncio.CancelledError:
                pass
            self._manager_task = None

    async def status(self) -> dict[str, Any]:
        control = await self.repository.get_trading_control()
        return {
            "auto_order_enabled": self.enabled,
            "position_management_enabled": bool(self.settings.position_management_enabled),
            "paused": bool(control.get("paused")),
            "pause_reason": control.get("reason"),
            "manager_running": bool(self._manager_task and not self._manager_task.done()),
            "settle": self.settings.gate_settle,
            "margin_mode": self.settings.gate_margin_mode,
        }

    async def pause(self, reason: str = "manual pause") -> dict[str, Any]:
        return await self.repository.set_trading_paused(True, reason)

    async def resume(self) -> dict[str, Any]:
        return await self.repository.set_trading_paused(False, None)

    async def process_scan(self, result: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "disabled", "orders": []}
        control = await self.repository.get_trading_control()
        if control.get("paused"):
            return {"status": "paused", "reason": control.get("reason"), "orders": []}
        candidates = list(result.get("rankings", {}).get("combined", []))
        if not candidates:
            return {"status": "no_candidates", "orders": []}
        async with self._order_lock:
            return await self._process_candidates(candidates)

    async def _process_candidates(self, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        positions = await self.gate.rest.get_positions()
        open_orders = await self.gate.rest.get_open_orders()
        positions_by_contract = {
            str(item.get("contract", "")).upper(): item
            for item in positions
            if abs(_number(item.get("size"))) > 0
        }
        open_contracts = {
            str(item.get("contract", "")).upper()
            for item in open_orders
            if abs(_number(item.get("size"), 1.0)) > 0 or item.get("size") in (None, "")
        }
        driver_contracts = self._market_driver_contracts()
        driver_count = sum(1 for contract in positions_by_contract if contract in driver_contracts)
        total_count = len(positions_by_contract)
        contracts = await self._contracts()
        actions: list[dict[str, Any]] = []
        for candidate in candidates:
            contract = str(candidate.get("contract", "")).upper()
            if not contract:
                continue
            if contract in positions_by_contract:
                action = {"contract": contract, "status": "skipped_existing_position"}
                actions.append(action)
                await self._notify_order(action)
                continue
            if contract in open_contracts:
                action = {"contract": contract, "status": "skipped_existing_open_order"}
                actions.append(action)
                await self._notify_order(action)
                continue
            if total_count >= int(self.settings.max_total_positions):
                action = {"contract": contract, "status": "skipped_total_position_limit"}
                actions.append(action)
                await self._notify_order(action)
                continue
            if contract in driver_contracts and driver_count >= int(self.settings.max_market_driver_positions):
                action = {"contract": contract, "status": "skipped_market_driver_limit"}
                actions.append(action)
                await self._notify_order(action)
                continue
            info = contracts.get(contract)
            if info is None:
                action = {"contract": contract, "status": "skipped_contract_unavailable"}
                actions.append(action)
                await self._notify_order(action)
                continue
            try:
                action = await self._open_candidate(candidate, info)
                actions.append(action)
                if action.get("status") == "submitted":
                    total_count += 1
                    if contract in driver_contracts:
                        driver_count += 1
                    positions_by_contract[contract] = {"contract": contract, "size": action.get("size", 1)}
                    open_contracts.add(contract)
            except TradingRiskError as exc:
                action = {"contract": contract, "status": "rejected_risk", "code": exc.code, "error": str(exc)}
                actions.append(action)
                await self._notify_order(action)
            except Exception as exc:
                logger.exception("candidate order failed for %s", contract)
                action = {"contract": contract, "status": "failed", "error": type(exc).__name__}
                actions.append(action)
                await self._notify_order(action)
        return {"status": "completed", "orders": actions}

    async def _open_candidate(self, candidate: dict[str, Any], info: Any) -> dict[str, Any]:
        ticker = await self.gate.rest.get_ticker(info.name)
        price = _number(ticker.get("mark_price")) or _number(ticker.get("last"))
        if price <= 0:
            raise TradingRiskError("NO_ENTRY_PRICE", "Gate ticker has no usable price")
        plan = build_execution_plan({**candidate, "metrics": {**candidate.get("metrics", {}), "ticker": {**candidate.get("metrics", {}).get("ticker", {}), **ticker}}}, info, self.settings, price)
        notional = self._notional(info.name)
        tiers = None
        if self.settings.require_max_leverage:
            try:
                tiers = await self.gate.rest.get_risk_limit_tiers(info.name)
            except Exception:
                logger.warning("risk limit tiers unavailable for %s; using contract maximum", info.name)
        leverage = max_leverage_for_notional(info, tiers, notional)
        if leverage is None:
            tiers = await self.gate.rest.get_risk_limit_tiers(info.name)
            leverage = max_leverage_for_notional(info, tiers, notional)
        if leverage is None and self.settings.require_max_leverage:
            raise TradingRiskError("MAX_LEVERAGE_UNAVAILABLE", "Gate maximum leverage could not be detected")
        if leverage is None:
            leverage = 1.0
        await self.gate.rest.set_leverage(info.name, leverage, self.settings.gate_margin_mode)
        size, actual_notional = notional_for_contract(info, price, notional)
        client_id = f"t-auto-entry-{uuid.uuid4().hex[:12]}"
        body = {
            "contract": info.name,
            "size": signed_size(plan["side"], size),
            "iceberg": "0",
            "price": "0",
            "tif": "ioc",
            "text": client_id,
            "reduce_only": False,
            "pos_margin_mode": self.settings.gate_margin_mode,
            "market_order_slip_ratio": str(self.settings.gate_market_order_slip_ratio),
        }
        response = await self.gate.rest.place_futures_order(body)
        position = await self.gate.rest.get_position(info.name)
        actual_size = abs(_number((position or {}).get("size")))
        if actual_size <= 0:
            raise TradingRiskError("ENTRY_NOT_FILLED", "Gate did not report a live position after IOC entry")
        actual_entry = _number((position or {}).get("entry_price"), price)
        plan = build_execution_plan(
            {**candidate, "metrics": {**candidate.get("metrics", {}), "ticker": ticker}},
            info,
            self.settings,
            actual_entry,
        )
        position_key = f"{info.name}:{plan['side']}"
        try:
            await self._install_protection(plan, actual_size, position_key)
        except Exception:
            await self._emergency_close(info.name, position_key)
            raise
        managed = self._managed_payload(position_key, plan, actual_size, response, leverage)
        await self.repository.save_managed_position(managed)
        await self.repository.save_order_event(
            {
                "event_id": uuid.uuid4().hex,
                "client_order_id": client_id,
                "contract": info.name,
                "event_type": "entry_submitted",
                "created_at": _now(),
                "payload": {"response": response, "notional": actual_notional, "leverage": leverage, "plan": plan},
            }
        )
        action = {
            "contract": info.name,
            "status": "submitted",
            "side": plan["side"],
            "size": actual_size,
            "entry_price": actual_entry,
            "notional": actual_notional,
            "leverage": leverage,
            "stop_loss": plan["initial_stop"],
            "take_profits": plan["take_profits"],
            "position_key": position_key,
        }
        await self._notify_order(action)
        return action

    async def _install_protection(self, plan: dict[str, Any], entry_size: float, position_key: str) -> None:
        contract = plan["contract"]
        side = plan["side"]
        stop_id = await self._create_trigger(plan, "stop", plan["current_stop"], "0", position_key)
        plan["protection_order_ids"]["stop"] = stop_id
        try:
            for target in plan["take_profits"]:
                stage = target["stage"]
                size = partial_close_size(side, str(entry_size), target["percent"])
                plan["protection_order_ids"][stage] = await self._create_trigger(
                    plan, stage, target["price"], size, position_key
                )
        except Exception:
            raise TradingRiskError("PROTECTION_ORDER_FAILED", f"failed to install all protection orders for {contract}")

    async def _create_trigger(
        self, plan: dict[str, Any], kind: str, trigger_price: float, size: str, position_key: str
    ) -> str:
        side = plan["side"]
        is_stop = kind == "stop"
        rule = 2 if (side == "long") == is_stop else 1
        close_type = "close-long-position" if side == "long" else "close-short-position"
        partial_type = "plan-close-long-position" if side == "long" else "plan-close-short-position"
        order_type = close_type if is_stop else partial_type
        tag = "sl" if is_stop else kind.lower()
        text = f"t-auto-{tag}-{uuid.uuid5(uuid.NAMESPACE_URL, position_key + tag + str(trigger_price)).hex[:12]}"
        initial: dict[str, Any] = {
            "contract": plan["contract"],
            "size": 0 if is_stop else size,
            "price": "0",
            "tif": "ioc",
            "reduce_only": True,
            "text": text,
        }
        if is_stop:
            initial["close"] = True
        trigger: dict[str, Any] = {
            "strategy_type": 0,
            "price_type": 1,
            "price": str(trigger_price),
            "rule": rule,
        }
        expiration = int(self.settings.order_trigger_expiration_seconds)
        if expiration > 0:
            trigger["expiration"] = expiration
        response = await self.gate.rest.create_price_order(
            {
                "initial": initial,
                "trigger": trigger,
                "order_type": order_type,
                "pos_margin_mode": self.settings.gate_margin_mode,
            }
        )
        order_id = _order_id(response)
        if not order_id:
            raise TradingRiskError("PROTECTION_ORDER_ID_MISSING", f"Gate did not return an order id for {kind}")
        return order_id

    async def _emergency_close(self, contract: str, position_key: str) -> None:
        try:
            await self.gate.rest.place_futures_order(
                {
                    "contract": contract,
                    "size": 0,
                    "price": "0",
                    "tif": "ioc",
                    "close": True,
                    "reduce_only": True,
                    "text": f"t-auto-emergency-{uuid.uuid4().hex[:12]}",
                    "pos_margin_mode": self.settings.gate_margin_mode,
                }
            )
        except Exception:
            logger.exception("emergency close failed for %s", position_key)

    async def manage_once(self) -> dict[str, Any]:
        positions = await self.gate.rest.get_positions()
        active = {
            str(item.get("contract", "")).upper(): item
            for item in positions
            if abs(_number(item.get("size"))) > 0
        }
        contracts = await self._contracts()
        managed = await self.repository.list_managed_positions(active_only=True)
        managed_keys = {item.get("position_key") for item in managed}
        actions: list[dict[str, Any]] = []
        for contract, position in active.items():
            size = _number(position.get("size"))
            side = "long" if size > 0 else "short"
            key = f"{contract}:{side}"
            info = contracts.get(contract)
            if info is None:
                continue
            try:
                ticker, context = await self._market_context(contract, info)
            except Exception as exc:
                logger.warning("market data invalid for managed position %s: %s", contract, type(exc).__name__)
                actions.append({"contract": contract, "status": "data_invalid", "error": type(exc).__name__})
                continue
            plan_record = await self.repository.get_managed_position(key)
            if plan_record is None:
                plan = self._plan_from_position(position, ticker, context, info)
                try:
                    await self._install_protection(plan, abs(size), key)
                except Exception as exc:
                    actions.append({"contract": contract, "status": "protection_failed", "error": type(exc).__name__})
                    continue
                plan_record = self._managed_payload(key, plan, abs(size), {}, _number(position.get("lever"), 0))
                await self.repository.save_managed_position(plan_record)
                actions.append({"contract": contract, "status": "adopted_and_protected"})
            else:
                try:
                    action = await self._manage_position(plan_record, position, ticker, context, info)
                    actions.append(action)
                except Exception as exc:
                    logger.exception("managed position update failed for %s", contract)
                    actions.append({"contract": contract, "status": "data_invalid", "error": type(exc).__name__})
            managed_keys.discard(key)
        for key in managed_keys:
            record = await self.repository.get_managed_position(key)
            if record:
                record["status"] = "closed"
                record["closed_at"] = _now().isoformat()
                record["updated_at"] = _now().isoformat()
                await self.repository.save_managed_position(record)
                action = {"position_key": key, "status": "closed"}
                actions.append(action)
                await self._notify_order(action)
        return {"status": "completed", "actions": actions}

    async def _manage_position(
        self, record: dict[str, Any], position: dict[str, Any], ticker: dict[str, Any], context: dict[str, Any], info: Any
    ) -> dict[str, Any]:
        plan = record["plan"]
        await self._ensure_protection(plan, abs(_number(position.get("size"))), record["position_key"])
        price = _number(ticker.get("mark_price")) or _number(ticker.get("last"))
        entry = _number(position.get("entry_price"), _number(plan.get("entry_price")))
        risk_distance = _number(plan.get("initial_risk_distance"))
        signed_move = price - entry if plan["side"] == "long" else entry - price
        current_r = signed_move / risk_distance if risk_distance > 0 else 0.0
        plan["current_r_multiple"] = current_r
        if current_r >= 3:
            plan["phase"] = "RUNNER_MANAGEMENT"
        elif current_r >= 2.5:
            plan["phase"] = "ACCELERATED_TRAILING"
        elif current_r >= 2:
            plan["phase"] = "STRUCTURE_TRAILING"
        elif current_r >= 1:
            plan["phase"] = "BREAK_EVEN_ELIGIBLE"
        elif current_r >= 0.75:
            plan["phase"] = "FIRST_PROTECTION"
        else:
            plan["phase"] = "INITIAL_RISK"
        for target in plan.get("take_profits", []):
            reached = price >= target["price"] if plan["side"] == "long" else price <= target["price"]
            if reached and target["stage"] not in plan["completed_stages"]:
                plan["completed_stages"].append(target["stage"])

        candidate_stop = _number(plan.get("current_stop"))
        atr15 = _number(context.get("atr15"), _number(plan.get("atr15")))
        atr5 = _number(context.get("atr5"), _number(plan.get("atr5"), atr15))
        if current_r >= 1:
            candidate_stop = max(candidate_stop, entry) if plan["side"] == "long" else min(candidate_stop, entry)
        if current_r >= 2:
            if plan["side"] == "long" and context.get("recent_low15"):
                candidate_stop = max(candidate_stop, context["recent_low15"] - 0.7 * atr15)
            if plan["side"] == "short" and context.get("recent_high15"):
                candidate_stop = min(candidate_stop, context["recent_high15"] + 0.7 * atr15)
        if current_r >= 2.5:
            if plan["side"] == "long" and context.get("recent_low5"):
                candidate_stop = max(candidate_stop, context["recent_low5"] - 0.7 * atr5)
            if plan["side"] == "short" and context.get("recent_high5"):
                candidate_stop = min(candidate_stop, context["recent_high5"] + 0.7 * atr5)
        stop_candidate_better = self._stop_is_better(plan["side"], candidate_stop, _number(plan.get("current_stop")))
        stop_moved = False
        if stop_candidate_better and abs(candidate_stop - _number(plan.get("current_stop"))) >= 0.15 * atr15:
            old_id = plan.get("protection_order_ids", {}).get("stop")
            new_id = await self._create_trigger(plan, "stop", candidate_stop, "0", record["position_key"])
            if old_id:
                try:
                    await self.gate.rest.cancel_price_order(old_id)
                except Exception:
                    logger.warning("failed to cancel previous stop %s", old_id)
            plan["protection_order_ids"]["stop"] = new_id
            plan["current_stop"] = candidate_stop
            plan["last_stop_update"] = _now().isoformat()
            stop_moved = True
        tp_moved = await self._maybe_extend_take_profit(record, plan, price, context)
        record["plan"] = plan
        record["current_size"] = abs(_number(position.get("size")))
        record["updated_at"] = _now().isoformat()
        await self.repository.save_managed_position(record)
        return {
            "contract": record["contract"],
            "status": "managed",
            "phase": plan["phase"],
            "current_r": current_r,
            "stop_changed": stop_moved,
            "take_profit_changed": tp_moved,
            "current_stop": plan.get("current_stop"),
        }

    async def _ensure_protection(self, plan: dict[str, Any], entry_size: float, position_key: str) -> None:
        open_orders = await self.gate.rest.get_price_orders(status="open", contract=plan["contract"])
        open_ids = {_order_id(item) for item in open_orders}
        ids = plan.setdefault("protection_order_ids", {})
        if ids.get("stop") not in open_ids:
            ids["stop"] = await self._create_trigger(plan, "stop", plan["current_stop"], "0", position_key)
        for target in plan.get("take_profits", []):
            stage = target["stage"]
            if stage in plan.get("completed_stages", []):
                continue
            if ids.get(stage) not in open_ids:
                ids[stage] = await self._create_trigger(
                    plan,
                    stage,
                    target["price"],
                    partial_close_size(plan["side"], str(entry_size), target["percent"]),
                    position_key,
                )

    async def _management_loop(self) -> None:
        while self._running:
            started = time.monotonic()
            try:
                await self.manage_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("position management cycle failed")
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.5, float(self.settings.position_manager_interval_seconds) - elapsed))

    async def _maybe_extend_take_profit(
        self, record: dict[str, Any], plan: dict[str, Any], price: float, context: dict[str, Any]
    ) -> bool:
        if plan.get("current_r_multiple", 0) < 2.5:
            return False
        last_update = plan.get("last_take_profit_update")
        if last_update:
            try:
                if (_now() - datetime.fromisoformat(last_update)).total_seconds() < 900:
                    return False
            except ValueError:
                pass
        atr15 = _number(context.get("atr15"), _number(plan.get("atr15")))
        if atr15 <= 0:
            return False
        candidates = [target for target in plan.get("take_profits", []) if target["stage"] not in plan.get("completed_stages", [])]
        if not candidates:
            return False
        target = candidates[-1]
        old_price = _number(target.get("price"))
        distance = old_price - price if plan["side"] == "long" else price - old_price
        if distance < 0 or distance > 0.2 * atr15:
            return False
        new_price = price + 1.0 * atr15 if plan["side"] == "long" else price - 1.0 * atr15
        if plan["side"] == "long":
            new_price = max(new_price, old_price + 0.2 * atr15)
        else:
            new_price = min(new_price, old_price - 0.2 * atr15)
        new_id = await self._create_trigger(
            plan,
            target["stage"],
            new_price,
            partial_close_size(plan["side"], str(record.get("current_size", 0)), target["percent"]),
            record["position_key"],
        )
        old_id = plan.get("protection_order_ids", {}).get(target["stage"])
        if old_id:
            try:
                await self.gate.rest.cancel_price_order(old_id)
            except Exception:
                logger.warning("failed to cancel previous take-profit %s", old_id)
        plan["protection_order_ids"][target["stage"]] = new_id
        target["price"] = new_price
        plan["last_take_profit_update"] = _now().isoformat()
        return True

    async def _market_context(self, contract: str, info: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        ticker = await self.gate.rest.get_ticker(contract)
        cached = self._market_cache.get(contract)
        if cached and time.monotonic() - cached[0] < float(self.settings.position_market_refresh_seconds):
            return ticker, cached[1]
        raw15, raw5 = await asyncio.gather(
            self.gate.rest.get_candlesticks(contract, "15m", limit=100),
            self.gate.rest.get_candlesticks(contract, "5m", limit=100),
        )
        candles15 = closed_candles(normalize_candles(raw15, info.quanto_multiplier), "15m")
        candles5 = closed_candles(normalize_candles(raw5, info.quanto_multiplier), "5m")
        context: dict[str, Any] = {}
        for label, candles in (("15", candles15), ("5", candles5)):
            frame = pd.DataFrame([item.model_dump() for item in candles])
            if frame.empty or len(frame) < 20:
                continue
            high = pd.to_numeric(frame["high"])
            low = pd.to_numeric(frame["low"])
            close = pd.to_numeric(frame["close"])
            atr_series = atr(high, low, close)
            context[f"atr{label}"] = float(atr_series.iloc[-1]) if pd.notna(atr_series.iloc[-1]) else None
            context[f"recent_low{label}"] = float(low.iloc[-21:-1].min())
            context[f"recent_high{label}"] = float(high.iloc[-21:-1].max())
        self._market_cache[contract] = (time.monotonic(), context)
        return ticker, context

    def _plan_from_position(
        self, position: dict[str, Any], ticker: dict[str, Any], context: dict[str, Any], info: Any
    ) -> dict[str, Any]:
        side = "long" if _number(position.get("size")) > 0 else "short"
        metrics = {
            "ticker": ticker,
            "15m": {"atr": context.get("atr15"), "recent_low": context.get("recent_low15"), "recent_high": context.get("recent_high15")},
            "5m": {"atr": context.get("atr5")},
        }
        return build_execution_plan({"direction": side, "metrics": metrics}, info, self.settings, _number(position.get("entry_price")))

    async def _contracts(self) -> dict[str, Any]:
        if time.monotonic() - self._contract_cache_at < 60 and self._contract_cache:
            return self._contract_cache
        contracts = await self.gate.rest.get_contracts()
        self._contract_cache = {str(item.name).upper(): item for item in [self._normalize_contract(item) for item in contracts]}
        self._contract_cache_at = time.monotonic()
        return self._contract_cache

    @staticmethod
    def _normalize_contract(raw: dict[str, Any]) -> Any:
        from app.gate.normalizer import normalize_contract

        return normalize_contract(raw)

    def _notional(self, contract: str) -> float:
        upper = contract.upper()
        if upper in {"BTC_USDT", "ETH_USDT"}:
            return float(self.settings.btc_eth_notional_usdt)
        if upper in self._market_driver_contracts():
            return float(self.settings.market_driver_notional_usdt)
        return float(self.settings.regular_alt_notional_usdt)

    def _market_driver_contracts(self) -> set[str]:
        return {item.strip().upper() for item in str(self.settings.market_driver_contracts).split(",") if item.strip()}

    @staticmethod
    def _stop_is_better(side: str, new: float, old: float) -> bool:
        if old <= 0 or new <= 0:
            return False
        return new > old if side == "long" else new < old

    @staticmethod
    def _managed_payload(
        position_key: str, plan: dict[str, Any], size: float, entry_response: dict[str, Any], leverage: float
    ) -> dict[str, Any]:
        return {
            "position_key": position_key,
            "contract": plan["contract"],
            "side": plan["side"],
            "status": "active",
            "current_size": size,
            "leverage": leverage,
            "entry_response": entry_response,
            "plan": plan,
            "updated_at": _now(),
        }

    async def _notify_order(self, action: dict[str, Any]) -> None:
        if self.notifier and hasattr(self.notifier, "send_order"):
            await self.notifier.send_order(action)

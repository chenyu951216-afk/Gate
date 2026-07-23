import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

import pandas as pd

from app.gate.normalizer import closed_candles, normalize_candles
from app.indicators.atr import atr
from app.trading.risk import (
    TradingRiskError,
    build_execution_plan,
    max_leverage_for_notional,
    notional_from_size,
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


def _decimal_text(value: float | Decimal) -> str:
    text = format(Decimal(str(value)), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


class TradingService:
    """Connect Gate scanner results to Bitget orders and keep protections alive.

    The scanner remains the source of entry candidates. This service only acts
    on qualified scanner results and never invents a symbol or direction.
    """

    def __init__(self, execution_client: Any, repository: Any, settings: Any, notifier: Any | None = None):
        # Keep the attribute name for compatibility with existing service
        # internals/tests; it is always the Bitget execution client at runtime.
        self.gate = execution_client
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
        mode = str(control.get("mode") or self.settings.trading_mode).lower()
        return {
            "auto_order_enabled": self.enabled,
            "position_management_enabled": bool(self.settings.position_management_enabled),
            "paused": bool(control.get("paused")),
            "pause_reason": control.get("reason"),
            "manager_running": bool(self._manager_task and not self._manager_task.done()),
            "exchange": "bitget",
            "settle": str(getattr(self.settings, "bitget_margin_coin", "USDT")).lower(),
            "margin_mode": "cross",
            "position_mode": "single",
            "mode": mode,
            "mode_label": "測試模式（名目金額 1/10）" if mode == "test" else "正式模式",
            "notional_multiplier": float(self.settings.test_mode_notional_multiplier) if mode == "test" else 1.0,
        }

    async def pause(self, reason: str = "manual pause") -> dict[str, Any]:
        return await self.repository.set_trading_paused(True, reason)

    async def resume(self) -> dict[str, Any]:
        return await self.repository.set_trading_paused(False, None)

    async def set_mode(self, mode: str) -> dict[str, Any]:
        return await self.repository.set_trading_mode(mode)

    async def process_scan(self, result: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "disabled", "orders": []}
        control = await self.repository.get_trading_control()
        self._runtime_mode = str(control.get("mode") or self.settings.trading_mode).lower()
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
        all_open_contracts = set(positions_by_contract) | open_contracts
        driver_count = sum(1 for contract in all_open_contracts if contract in driver_contracts)
        total_count = len(all_open_contracts)
        contracts = await self._contracts()
        actions: list[dict[str, Any]] = []
        batch_direction_counts = {"long": 0, "short": 0}
        for candidate in candidates:
            contract = str(candidate.get("contract", "")).upper()
            if not contract:
                continue
            action: dict[str, Any]
            if contract in positions_by_contract:
                action = {"contract": contract, "status": "skipped_existing_position"}
                actions.append(action)
                continue
            if contract in open_contracts:
                action = {"contract": contract, "status": "skipped_existing_open_order"}
                actions.append(action)
                continue
            direction = str(candidate.get("direction", "")).lower()
            if direction in batch_direction_counts and batch_direction_counts[direction] >= int(self.settings.max_same_direction_orders_per_batch):
                action = {
                    "contract": contract,
                    "status": "skipped_batch_direction_limit",
                    "direction": direction,
                    "limit": int(self.settings.max_same_direction_orders_per_batch),
                }
                actions.append(action)
                continue
            if total_count >= int(self.settings.max_total_positions):
                action = {"contract": contract, "status": "skipped_total_position_limit"}
                actions.append(action)
                continue
            if contract in driver_contracts and driver_count >= int(self.settings.max_market_driver_positions):
                action = {"contract": contract, "status": "skipped_market_driver_limit"}
                actions.append(action)
                continue
            info = self._resolve_execution_contract(contract, contracts)
            if info is None:
                action = {
                    "contract": contract,
                    "status": "skipped_contract_unavailable",
                    "code": "BITGET_CONTRACT_UNAVAILABLE",
                    "error": "Gate candidate has no exact active Bitget USDT perpetual match",
                }
                actions.append(action)
                continue
            try:
                action = await self._open_candidate(candidate, info)
                actions.append(action)
                if action.get("status") in {"submitted", "limit_order_open"}:
                    total_count += 1
                    if contract in driver_contracts:
                        driver_count += 1
                    positions_by_contract[contract] = {"contract": contract, "size": action.get("size", 1)}
                    open_contracts.add(contract)
                    if direction in batch_direction_counts:
                        batch_direction_counts[direction] += 1
            except TradingRiskError as exc:
                action = {"contract": contract, "status": "rejected_risk", "code": exc.code, "error": str(exc)}
                actions.append(action)
            except Exception as exc:
                logger.exception("candidate order failed for %s", contract)
                action = {
                    "contract": contract,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc) or type(exc).__name__,
                }
                actions.append(action)
        return {"status": "completed", "orders": actions}

    @staticmethod
    def _limit_entry_price(ticker: dict[str, Any], side: str, mark_price: float, info: Any, offset_pct: float) -> float:
        bid = _number(ticker.get("highest_bid"))
        ask = _number(ticker.get("lowest_ask"))
        reference = bid if side == "long" and bid > 0 else ask if side == "short" and ask > 0 else mark_price
        offset = min(0.01, max(0.0, float(offset_pct)))
        raw_price = reference * (1 - offset) if side == "long" else reference * (1 + offset)
        tick = _number(getattr(info, "order_price_round", None))
        if tick > 0:
            units = Decimal(str(raw_price)) / Decimal(str(tick))
            rounding = ROUND_FLOOR if side == "long" else ROUND_CEILING
            raw_price = float(units.to_integral_value(rounding=rounding) * Decimal(str(tick)))
        if raw_price <= 0:
            raise TradingRiskError("INVALID_LIMIT_PRICE", "Bitget limit entry price is invalid")
        return raw_price

    async def _open_candidate(self, candidate: dict[str, Any], info: Any) -> dict[str, Any]:
        if str(self.settings.entry_order_mode).lower() != "limit":
            raise TradingRiskError("LIMIT_ENTRY_REQUIRED", "entry order mode must be limit")
        ticker = await self.gate.rest.get_ticker(info.name)
        mark_price = _number(ticker.get("mark_price")) or _number(ticker.get("last"))
        if mark_price <= 0:
            raise TradingRiskError("NO_ENTRY_PRICE", "Bitget ticker has no usable price")
        side = str(candidate.get("direction", "")).lower()
        if side not in {"long", "short"}:
            raise TradingRiskError("INVALID_DIRECTION", "ranking direction must be long or short")
        limit_price = self._limit_entry_price(
            ticker,
            side,
            mark_price,
            info,
            float(self.settings.limit_entry_offset_pct),
        )
        notional = self._notional(info.name)
        size, actual_notional = notional_for_contract(info, limit_price, notional)
        ticker_metrics = {**candidate.get("metrics", {}), "ticker": {**candidate.get("metrics", {}).get("ticker", {}), **ticker}}
        plan = build_execution_plan(
            {**candidate, "metrics": ticker_metrics},
            info,
            self.settings,
            limit_price,
            risk_notional_usdt=actual_notional,
        )
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
            raise TradingRiskError("MAX_LEVERAGE_UNAVAILABLE", "Bitget maximum leverage could not be detected")
        if leverage is None:
            leverage = 1.0
        await self._ensure_single_position_mode()
        await self._ensure_cross_margin(info.name, leverage)
        plan["margin_mode"] = "cross"
        client_id = f"t-auto-entry-{uuid.uuid4().hex[:12]}"
        body = {
            "contract": info.name,
            "size": signed_size(plan["side"], size),
            "iceberg": "0",
            "price": _decimal_text(limit_price),
            "tif": "gtc",
            "text": client_id,
            "reduce_only": False,
            "pos_margin_mode": "cross",
            # Bitget can bind an initial stop to the entry order itself. This is
            # the first exchange-side safety layer; price_orders below add the
            # independently managed stop and partial take-profits after fill.
            "tpsl_sl_trigger_price": self._protection_price_text(plan, "stop", plan["initial_stop"]),
            # Bitget accepts a preset TP and SL on the entry order itself.
            # The post-fill exchange plans below add the independent
            # multi-stage protections as a second layer.
            "tpsl_tp_trigger_price": self._protection_price_text(plan, "TP1", plan["take_profits"][0]["price"]),
        }
        response = await self.gate.rest.place_futures_order(body)
        entry_order_id = _order_id(response)
        if not entry_order_id:
            raise TradingRiskError("ENTRY_ORDER_ID_MISSING", "Bitget did not return a limit order id")
        try:
            position = await self.gate.rest.get_position(info.name)
        except Exception as exc:
            logger.warning("position lookup delayed after limit order %s: %s", entry_order_id, type(exc).__name__)
            position = None
        actual_size = abs(_number((position or {}).get("size")))
        if actual_size > 0 and self._position_margin_mode(position) != "cross":
            await self._emergency_close(info.name, f"{info.name}:{side}")
            raise TradingRiskError(
                "CROSS_MARGIN_NOT_CONFIRMED",
                f"Bitget filled {info.name} outside cross margin; the position was closed for safety",
            )
        if actual_size <= 0:
            await self.repository.save_order_event(
                {
                    "event_id": uuid.uuid4().hex,
                    "client_order_id": client_id,
                    "contract": info.name,
                    "event_type": "entry_limit_open",
                    "created_at": _now(),
                    "payload": {
                        "response": response,
                        "order_id": entry_order_id,
                        "entry_limit_price": limit_price,
                        "notional": actual_notional,
                        "leverage": leverage,
                        "plan": plan,
                    },
                }
            )
            action = {
                "contract": info.name,
                "status": "limit_order_open",
                "side": plan["side"],
                "size": float(size),
                "entry_limit_price": limit_price,
                "notional": actual_notional,
                "leverage": leverage,
                "entry_order_id": entry_order_id,
                "pending_entry": True,
                "protection_status": "pending_until_fill",
            }
            return action
        try:
            await self.gate.rest.cancel_futures_order(entry_order_id)
        except Exception:
            logger.info("entry order %s already filled or cancelled", entry_order_id)
        actual_entry = _number((position or {}).get("entry_price"), limit_price)
        actual_notional = notional_from_size(info, actual_entry, actual_size)
        position_key = f"{info.name}:{side}"
        try:
            plan = build_execution_plan(
                {**candidate, "metrics": ticker_metrics},
                info,
                self.settings,
                actual_entry,
                risk_notional_usdt=actual_notional,
            )
            position_key = f"{info.name}:{plan['side']}"
        except Exception:
            await self._emergency_close(info.name, position_key)
            raise
        protection_status = "exchange"
        protection_error = None
        try:
            await self._install_protection(plan, actual_size, position_key)
        except Exception as exc:
            protection_status = "backend_fallback"
            protection_error = str(exc) or type(exc).__name__
            logger.exception("exchange protection installation failed for %s; backend fallback enabled", info.name)
        managed = self._managed_payload(position_key, plan, actual_size, response, leverage)
        managed["protection_status"] = protection_status
        if protection_error:
            managed["protection_error"] = protection_error
        await self.repository.save_managed_position(managed)
        await self.repository.save_order_event(
            {
                "event_id": uuid.uuid4().hex,
                "client_order_id": client_id,
                "contract": info.name,
                "event_type": "entry_submitted",
                "created_at": _now(),
                "payload": {
                    "response": response,
                    "notional": actual_notional,
                    "leverage": leverage,
                    "plan": plan,
                    "protection_status": protection_status,
                    "protection_error": protection_error,
                },
            }
        )
        action = {
            "contract": info.name,
            "status": "submitted",
            "side": plan["side"],
            "size": actual_size,
            "entry_price": actual_entry,
            "entry_order_id": entry_order_id,
            "notional": actual_notional,
            "leverage": leverage,
            "margin_mode": plan.get("margin_mode", "cross"),
            "stop_loss": plan["initial_stop"],
            "take_profits": plan["take_profits"],
            "position_key": position_key,
            "protection_status": protection_status,
        }
        if protection_error:
            action["protection_error"] = protection_error
        await self._notify_order(action)
        return action

    @staticmethod
    def _position_margin_mode(payload: dict[str, Any] | None) -> str | None:
        if not payload:
            return None
        raw = payload.get("pos_margin_mode") or payload.get("margin_mode")
        mode = str(raw or "").strip().lower()
        return mode if mode in {"cross", "isolated"} else None

    @classmethod
    def _response_confirms_cross(cls, payload: dict[str, Any] | None) -> bool:
        mode = cls._position_margin_mode(payload)
        if mode:
            return mode == "cross"
        if not payload or "leverage" not in payload:
            return False
        return str(payload.get("leverage")).strip() in {"0", "0.0"} and payload.get("cross_leverage_limit") not in (
            None,
            "",
        )

    async def _ensure_single_position_mode(self) -> None:
        """Ensure Bitget is in one-way position mode before submitting."""
        try:
            positions = await self.gate.rest.get_positions()
        except Exception as exc:
            raise TradingRiskError(
                "POSITION_MODE_NOT_CONFIRMED",
                f"cannot read Bitget positions for mode verification: {type(exc).__name__}: {exc}",
            ) from exc
        try:
            open_orders = await self.gate.rest.get_open_orders()
        except Exception as exc:
            raise TradingRiskError(
                "POSITION_MODE_NOT_CONFIRMED",
                f"cannot read Bitget open orders for mode verification: {type(exc).__name__}: {exc}",
            ) from exc
        active_modes = {
            str(item.get("mode") or "").lower()
            for item in positions
            if abs(_number(item.get("size"))) > 0
        }
        active_modes.update(
            str(item.get("mode") or "").lower()
            for item in open_orders
            if item.get("contract")
        )
        if active_modes & {"dual", "dual_long", "dual_short", "dual_plus"}:
            raise TradingRiskError(
                "POSITION_MODE_NOT_CONFIRMED",
                "Bitget has an existing hedge position; close or convert it manually before new orders",
            )
        # Bitget refuses the global mode endpoint while *any* position/order is held.
        # If every active position already reports one-way mode, the account is
        # ready and there is no reason to call that endpoint again.
        if active_modes and active_modes <= {"single"}:
            return
        try:
            account = await self.gate.rest.get_account()
        except Exception as exc:
            raise TradingRiskError(
                "POSITION_MODE_NOT_CONFIRMED",
                f"cannot read Bitget position mode: {type(exc).__name__}: {exc}",
            ) from exc
        raw_mode = str(account.get("position_mode") or "").lower()
        if raw_mode == "single" or (not raw_mode and account.get("in_dual_mode") is False):
            return
        if account.get("in_dual_mode") is True or raw_mode in {"dual", "dual_plus"} or not raw_mode:
            try:
                response = await self.gate.rest.set_position_mode("single")
            except Exception as exc:
                raise TradingRiskError(
                    "POSITION_MODE_NOT_CONFIRMED",
                    f"Bitget could not switch to one-way position mode: {type(exc).__name__}: {exc}",
                ) from exc
            response_mode = str(response.get("position_mode") or "").lower()
            if response.get("in_dual_mode") is True or response_mode in {"dual", "dual_plus"}:
                raise TradingRiskError(
                    "POSITION_MODE_NOT_CONFIRMED",
                    "Bitget still reports hedge mode after requesting one-way mode",
                )
            if response.get("in_dual_mode") is False or response_mode == "single":
                return
            try:
                verified = await self.gate.rest.get_account()
            except Exception as exc:
                raise TradingRiskError(
                    "POSITION_MODE_NOT_CONFIRMED",
                    f"cannot verify Bitget one-way position mode: {type(exc).__name__}: {exc}",
                ) from exc
            if verified.get("in_dual_mode") is not False and str(verified.get("position_mode") or "").lower() != "single":
                raise TradingRiskError(
                    "POSITION_MODE_NOT_CONFIRMED",
                    "Bitget did not confirm one-way position mode",
                )
            return
        raise TradingRiskError(
            "POSITION_MODE_NOT_CONFIRMED",
            f"unsupported Bitget position mode: {raw_mode or 'unknown'}",
        )

    async def _ensure_cross_margin(self, contract: str, leverage: float) -> None:
        """Switch and verify Bitget crossed margin before an entry is submitted.

        Bitget exposes dedicated margin-mode, leverage and position-mode
        endpoints. The legacy method name is retained by the adapter as a
        second crossed-margin attempt.
        """
        errors: list[str] = []
        mode_response: dict[str, Any] | None = None
        leverage_response: dict[str, Any] | None = None
        try:
            mode_response = await self.gate.rest.set_position_margin_mode(contract, "cross")
        except Exception as exc:
            errors.append(f"set_margin_mode: {type(exc).__name__}: {exc}")
        try:
            leverage_response = await self.gate.rest.set_leverage(contract, leverage, "cross")
        except Exception as exc:
            errors.append(f"set_leverage: {type(exc).__name__}: {exc}")

        leverage_mode = self._position_margin_mode(leverage_response)
        mode_mode = self._position_margin_mode(mode_response)

        # Prefer the position returned by Bitget after the mode/leverage calls.
        # This is the only confirmation that describes the state which the
        # following entry order will actually inherit.  Some responses
        # return a successful response without `pos_margin_mode` in it.
        try:
            current = await self.gate.rest.get_position(contract)
            if current and self._response_confirms_cross(current):
                return
            if current:
                errors.append(f"verified_mode={self._position_margin_mode(current) or 'unknown'}")
        except Exception as exc:
            errors.append(f"verify_position: {type(exc).__name__}: {exc}")

        # If the read-back is unavailable (for example, a brand-new contract
        # with no position row yet), accept two independent successful Bitget
        # responses only when neither says isolated explicitly.
        if (
            mode_response
            and self._response_confirms_cross(mode_response)
            and leverage_response is not None
            and leverage_mode != "isolated"
        ) or (
            self._response_confirms_cross(leverage_response)
            and leverage_mode != "isolated"
            and mode_mode in (None, "cross")
        ):
            return

        try:
            legacy_response = await self.gate.rest.set_cross_leverage_legacy(contract, leverage)
        except Exception as exc:
            errors.append(f"legacy_cross_leverage: {type(exc).__name__}: {exc}")
            legacy_response = None

        try:
            current = await self.gate.rest.get_position(contract)
            if current and self._response_confirms_cross(current):
                return
            if current:
                errors.append(f"verified_legacy_mode={self._position_margin_mode(current) or 'unknown'}")
        except Exception as exc:
            errors.append(f"verify_legacy_position: {type(exc).__name__}: {exc}")
        if self._response_confirms_cross(legacy_response):
            return

        detail = "; ".join(errors) or "Bitget returned no crossed-margin confirmation"
        raise TradingRiskError("CROSS_MARGIN_NOT_CONFIRMED", f"cannot confirm cross margin for {contract}: {detail}")

    async def _install_protection(self, plan: dict[str, Any], entry_size: float, position_key: str) -> None:
        contract = plan["contract"]
        side = plan["side"]
        plan["entry_size"] = entry_size
        created_ids: list[str] = []
        try:
            stop_id = await self._create_trigger(plan, "stop", plan["current_stop"], "0", position_key)
            created_ids.append(stop_id)
            plan["protection_order_ids"]["stop"] = stop_id
            for target in plan["take_profits"]:
                stage = target["stage"]
                size = partial_close_size(side, str(entry_size), target["percent"])
                order_id = await self._create_trigger(
                    plan, stage, target["price"], size, position_key
                )
                created_ids.append(order_id)
                plan["protection_order_ids"][stage] = order_id
            await self._verify_exchange_protection(plan)
        except Exception as exc:
            for order_id in created_ids:
                try:
                    await self.gate.rest.cancel_price_order(order_id)
                except Exception:
                    logger.warning("failed to clean protection order %s for %s", order_id, contract)
            raise TradingRiskError(
                "PROTECTION_ORDER_FAILED",
                f"failed to install all protection orders for {contract}: {type(exc).__name__}: {exc}",
            ) from exc

    async def _create_trigger(
        self, plan: dict[str, Any], kind: str, trigger_price: float, size: str, position_key: str
    ) -> str:
        side = plan["side"]
        is_stop = kind == "stop"
        trigger_price = self._rounded_protection_price(plan, kind, trigger_price)
        rule = 2 if (side == "long") == is_stop else 1
        close_type = "close-long-position" if side == "long" else "close-short-position"
        partial_type = "plan-close-long-position" if side == "long" else "plan-close-short-position"
        order_type = close_type if is_stop else partial_type
        tag = "sl" if is_stop else kind.lower()
        text = f"t-auto-{tag}-{uuid.uuid5(uuid.NAMESPACE_URL, position_key + tag + str(trigger_price)).hex[:12]}"
        initial: dict[str, Any] = {
            "contract": plan["contract"],
            "price": "0",
            "tif": "ioc",
            "reduce_only": True,
            "text": text,
        }
        if is_stop:
            initial["size"] = 0
            initial["close"] = True
        elif plan.get("enable_decimal"):
            # The shared payload keeps Gate-style fields; the Bitget adapter
            # converts the base-coin quantity to its size multiplier.
            initial["amount"] = size
        else:
            # The price-order endpoint documents `size` as int64.  The normal
            # futures order endpoint accepts a numeric string, but passing the
            # same string here can make an exchange reject a valid TP.
            integer_size = int(Decimal(str(size)))
            if integer_size == 0:
                raise TradingRiskError(
                    "PROTECTION_SIZE_ZERO",
                    f"calculated protection size is zero for {kind} on {plan['contract']}",
                )
            initial["size"] = integer_size
        trigger: dict[str, Any] = {
            "strategy_type": 0,
            # The strategy uses the latest traded price as its trigger source.
            # Keeping this explicit avoids a mark/last tick mismatch on fast
            # moving contracts.
            "price_type": 0,
            "price": _decimal_text(trigger_price),
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
                "pos_margin_mode": plan.get("margin_mode", "cross"),
            }
        )
        order_id = _order_id(response)
        if not order_id:
            raise TradingRiskError("PROTECTION_ORDER_ID_MISSING", f"Bitget did not return an order id for {kind}")
        return order_id

    @staticmethod
    def _rounded_protection_price(plan: dict[str, Any], kind: str, trigger_price: float) -> float:
        side = plan["side"]
        is_stop = kind == "stop"
        tick = _number(plan.get("price_tick"))
        if tick <= 0:
            return trigger_price
        units = Decimal(str(trigger_price)) / Decimal(str(tick))
        if side == "long":
            rounding = ROUND_FLOOR if is_stop else ROUND_CEILING
        else:
            rounding = ROUND_CEILING if is_stop else ROUND_FLOOR
        return float(units.to_integral_value(rounding=rounding) * Decimal(str(tick)))

    def _protection_price_text(self, plan: dict[str, Any], kind: str, trigger_price: float) -> str:
        return _decimal_text(self._rounded_protection_price(plan, kind, trigger_price))

    async def _verify_exchange_protection(self, plan: dict[str, Any]) -> None:
        ids = plan.get("protection_order_ids", {})
        expected = {str(ids.get("stop"))} if ids.get("stop") not in (None, "") else set()
        expected.update(
            str(ids.get(target["stage"]))
            for target in plan.get("take_profits", [])
            if target["stage"] not in plan.get("completed_stages", [])
            and ids.get(target["stage"]) not in (None, "")
        )
        if not expected:
            raise TradingRiskError("PROTECTION_ORDER_ID_MISSING", f"no exchange protection ids for {plan['contract']}")
        for attempt in range(5):
            open_orders = await self.gate.rest.get_price_orders(status="open", contract=plan["contract"])
            visible = {_order_id(item) for item in open_orders}
            if expected.issubset(visible):
                return
            if attempt < 4:
                await asyncio.sleep(0.2 * (attempt + 1))
        missing = sorted(expected - visible)
        raise TradingRiskError(
            "PROTECTION_ORDER_NOT_CONFIRMED",
            f"Bitget did not confirm exchange protection orders for {plan['contract']}: {','.join(missing)}",
        )

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
                    "pos_margin_mode": "cross",
                }
            )
        except Exception:
            logger.exception("emergency close failed for %s", position_key)

    async def _monitor_pending_entries(self) -> list[dict[str, Any]]:
        open_orders = await self.gate.rest.get_open_orders()
        pending = [
            item
            for item in open_orders
            if str(item.get("text", "")).startswith("t-auto-entry-")
        ]
        if not pending:
            return []
        positions = await self.gate.rest.get_positions()
        active_contracts = {
            str(item.get("contract", "")).upper()
            for item in positions
            if abs(_number(item.get("size"))) > 0
        }
        now = time.time()
        actions: list[dict[str, Any]] = []
        for order in pending:
            contract = str(order.get("contract", "")).upper()
            order_id = _order_id(order)
            limit_price = _number(order.get("price"))
            if not contract or not order_id or limit_price <= 0:
                continue
            if contract in active_contracts:
                try:
                    await self.gate.rest.cancel_futures_order(order_id)
                except Exception:
                    logger.info("filled entry order %s has no remaining quantity", order_id)
                continue
            size = _number(order.get("size"))
            side = "long" if size > 0 else "short" if size < 0 else ""
            if not side:
                continue
            created_at = _number(order.get("create_time"))
            age = now - created_at if created_at > 0 else 0.0
            reason = None
            if age >= int(self.settings.limit_entry_timeout_seconds):
                reason = "LIMIT_ENTRY_TIMEOUT"
            else:
                try:
                    ticker = await self.gate.rest.get_ticker(contract)
                    current_price = _number(ticker.get("mark_price")) or _number(ticker.get("last"))
                except Exception as exc:
                    logger.warning("pending entry ticker unavailable for %s: %s", contract, type(exc).__name__)
                    continue
                directional_move = (
                    (current_price - limit_price) / limit_price
                    if side == "long"
                    else (limit_price - current_price) / limit_price
                )
                if directional_move >= float(self.settings.limit_entry_cancel_move_pct):
                    reason = "LIMIT_ENTRY_MOMENTUM"
            if reason is None:
                continue
            try:
                await self.gate.rest.cancel_futures_order(order_id)
                action = {
                    "contract": contract,
                    "status": "limit_order_cancelled",
                    "code": reason,
                    "side": side,
                    "entry_order_id": order_id,
                    "entry_limit_price": limit_price,
                    "age_seconds": round(age, 1),
                }
                actions.append(action)
            except Exception as exc:
                logger.warning("failed to cancel pending entry %s: %s", order_id, exc)
        return actions

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
                protection_status = "exchange"
                protection_error = None
                try:
                    await self._install_protection(plan, abs(size), key)
                except Exception as exc:
                    protection_status = "backend_fallback"
                    protection_error = str(exc) or type(exc).__name__
                    logger.exception(
                        "exchange protection installation failed while adopting %s; backend fallback enabled",
                        contract,
                    )
                plan_record = self._managed_payload(key, plan, abs(size), {}, _number(position.get("lever"), 0))
                plan_record["protection_status"] = protection_status
                if protection_error:
                    plan_record["protection_error"] = protection_error
                await self.repository.save_managed_position(plan_record)
                action = {
                    "contract": contract,
                    "status": "new_position_adopted",
                    "side": side,
                    "size": abs(size),
                    "entry_price": _number(position.get("entry_price")),
                    "margin_mode": plan.get("margin_mode"),
                    "stop_loss": plan.get("initial_stop"),
                    "take_profits": plan.get("take_profits", []),
                    "protection_status": protection_status,
                }
                if protection_error:
                    action["protection_error"] = protection_error
                actions.append(action)
                await self._notify_order(action)
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
        return {"status": "completed", "actions": actions}

    async def _manage_position(
        self, record: dict[str, Any], position: dict[str, Any], ticker: dict[str, Any], context: dict[str, Any], info: Any
    ) -> dict[str, Any]:
        plan = record["plan"]
        plan["margin_mode"] = self._position_margin_mode(position) or plan.get(
            "margin_mode", "cross"
        )
        price = _number(ticker.get("mark_price")) or _number(ticker.get("last"))
        entry = _number(position.get("entry_price"), _number(plan.get("entry_price")))
        protection_error = None
        try:
            missing_protection = await self._ensure_protection(
                plan, abs(_number(position.get("size"))), record["position_key"]
            )
        except Exception as exc:
            missing_protection = {"stop", *[target["stage"] for target in plan.get("take_profits", [])]}
            protection_error = f"{type(exc).__name__}: {exc}"
            logger.exception("exchange protection refresh failed for %s; backend fallback enabled", plan["contract"])

        all_protection_stages = {"stop", *[target["stage"] for target in plan.get("take_profits", [])]}
        if protection_error is None and not missing_protection:
            record["protection_status"] = "exchange"
            record.pop("protection_error", None)

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
        reached_stages = [
            target["stage"]
            for target in plan.get("take_profits", [])
            if (
                price >= target["price"]
                if plan["side"] == "long"
                else price <= target["price"]
            )
            and target["stage"] not in plan.get("completed_stages", [])
        ]

        fallback_actions: list[dict[str, Any]] = []
        fallback_stages = set(missing_protection)
        if protection_error is not None:
            fallback_stages = all_protection_stages
        if fallback_stages:
            fallback_actions = await self._backend_fallback_protection(
                plan,
                position,
                price,
                reached_stages,
                record["position_key"],
                protection_error,
                fallback_stages,
            )
            for action in fallback_actions:
                stage = action.get("stage")
                if stage and stage not in plan["completed_stages"]:
                    plan["completed_stages"].append(stage)

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
            try:
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
            except Exception as exc:
                protection_error = protection_error or f"{type(exc).__name__}: {exc}"
                logger.exception("exchange trailing stop update failed for %s", plan["contract"])

        try:
            tp_moved = await self._maybe_extend_take_profit(record, plan, price, context)
        except Exception as exc:
            tp_moved = False
            protection_error = protection_error or f"{type(exc).__name__}: {exc}"
            logger.exception("exchange take-profit update failed for %s", plan["contract"])

        if protection_error:
            record["protection_status"] = "backend_fallback"
            record["protection_error"] = protection_error
        record["plan"] = plan
        record["current_size"] = abs(_number(position.get("size")))
        record["updated_at"] = _now().isoformat()
        await self.repository.save_managed_position(record)
        result = {
            "contract": record["contract"],
            "status": "managed",
            "phase": plan["phase"],
            "current_r": current_r,
            "stop_changed": stop_moved,
            "take_profit_changed": tp_moved,
            "current_stop": plan.get("current_stop"),
            "protection_status": record.get("protection_status", "exchange"),
            "fallback_actions": fallback_actions,
        }
        if record.get("protection_error"):
            result["protection_error"] = record["protection_error"]
        return result

    async def _ensure_protection(self, plan: dict[str, Any], entry_size: float, position_key: str) -> set[str]:
        open_orders = await self.gate.rest.get_price_orders(status="open", contract=plan["contract"])
        open_ids = {_order_id(item) for item in open_orders}
        ids = plan.setdefault("protection_order_ids", {})
        missing: set[str] = set()
        if ids.get("stop") not in open_ids:
            missing.add("stop")
            ids["stop"] = await self._create_trigger(plan, "stop", plan["current_stop"], "0", position_key)
        for target in plan.get("take_profits", []):
            stage = target["stage"]
            if stage in plan.get("completed_stages", []):
                continue
            if ids.get(stage) not in open_ids:
                missing.add(stage)
                ids[stage] = await self._create_trigger(
                    plan,
                    stage,
                    target["price"],
                    partial_close_size(plan["side"], str(entry_size), target["percent"]),
                    position_key,
                )
        if missing:
            await self._verify_exchange_protection(plan)
        return missing

    async def _backend_fallback_protection(
        self,
        plan: dict[str, Any],
        position: dict[str, Any],
        price: float,
        reached_stages: list[str],
        position_key: str,
        protection_error: str | None,
        fallback_stages: set[str],
    ) -> list[dict[str, Any]]:
        """Last-resort reduce-only protection when exchange triggers are unavailable.

        Exchange trigger orders are always installed/maintained first. This path is
        intentionally only enabled after a protection error, a missing trigger, or
        a position that was adopted while exchange protection could not be installed.
        """
        fallback_size = abs(_number(position.get("size")))
        try:
            live_position = await self.gate.rest.get_position(plan["contract"])
            if live_position is None:
                fallback_size = 0.0
            else:
                fallback_size = abs(_number(live_position.get("size"), fallback_size))
        except Exception as exc:
            logger.warning("live position refresh failed for fallback %s: %s", position_key, type(exc).__name__)
        if fallback_size <= 0:
            return []

        reason = protection_error or "exchange protection trigger missing"
        actions: list[dict[str, Any]] = []
        current_stop = _number(plan.get("current_stop"))
        stop_hit = (
            price <= current_stop if plan["side"] == "long" else price >= current_stop
        )
        if stop_hit and "stop" in fallback_stages:
            await self.gate.rest.place_futures_order(
                {
                    "contract": plan["contract"],
                    "size": 0,
                    "price": "0",
                    "tif": "ioc",
                    "close": True,
                    "reduce_only": True,
                    "text": f"t-auto-fallback-sl-{uuid.uuid4().hex[:12]}",
                    "pos_margin_mode": plan.get("margin_mode", "cross"),
                }
            )
            actions.append({"stage": "STOP", "status": "fallback_stop_submitted", "reason": reason})
            return actions

        for target in plan.get("take_profits", []):
            stage = target["stage"]
            if stage not in reached_stages or stage not in fallback_stages:
                continue
            size = partial_close_size(plan["side"], str(fallback_size), target["percent"])
            if abs(_number(size)) <= 0:
                continue
            await self.gate.rest.place_futures_order(
                {
                    "contract": plan["contract"],
                    "size": size,
                    "price": "0",
                    "tif": "ioc",
                    "reduce_only": True,
                    "text": f"t-auto-fallback-{stage.lower()}-{uuid.uuid4().hex[:10]}",
                    "pos_margin_mode": plan.get("margin_mode", "cross"),
                }
            )
            actions.append({"stage": stage, "status": "fallback_take_profit_submitted", "reason": reason})
            fallback_size = max(0.0, fallback_size - abs(_number(size)))
        return actions

    async def _management_loop(self) -> None:
        while self._running:
            started = time.monotonic()
            try:
                try:
                    await self._monitor_pending_entries()
                except Exception:
                    logger.exception("pending entry monitoring cycle failed")
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
        plan = build_execution_plan({"direction": side, "metrics": metrics}, info, self.settings, _number(position.get("entry_price")))
        plan["margin_mode"] = self._position_margin_mode(position) or "cross"
        return plan

    async def _contracts(self) -> dict[str, Any]:
        if time.monotonic() - self._contract_cache_at < 60 and self._contract_cache:
            return self._contract_cache
        contracts = await self.gate.rest.get_contracts()
        self._contract_cache = {str(item.name).upper(): item for item in [self._normalize_contract(item) for item in contracts]}
        self._contract_cache_at = time.monotonic()
        return self._contract_cache

    @staticmethod
    def _contract_identity(value: str) -> tuple[str, str] | None:
        normalized = str(value or "").upper().replace("-", "_").replace("/", "_")
        if normalized.endswith("_USDT"):
            return normalized[:-5], "USDT"
        if normalized.endswith("USDT"):
            return normalized[:-4], "USDT"
        return None

    @classmethod
    def _resolve_execution_contract(cls, source_contract: str, contracts: dict[str, Any]) -> Any | None:
        """Match a Gate candidate to exactly one Bitget contract.

        This is deliberately an exact base-asset comparison.  It accepts
        formatting differences such as ``BTC_USDT`` versus ``BTCUSDT`` after
        normalization, but rejects guessed aliases and multiplier prefixes.
        """
        identity = cls._contract_identity(source_contract)
        if identity is None:
            return None
        matches = [
            info
            for info in contracts.values()
            if cls._contract_identity(str(getattr(info, "name", ""))) == identity
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _normalize_contract(raw: dict[str, Any]) -> Any:
        from app.gate.normalizer import normalize_contract

        return normalize_contract(raw)

    def _notional(self, contract: str) -> float:
        upper = contract.upper()
        if upper in {"BTC_USDT", "ETH_USDT"}:
            base = float(self.settings.btc_eth_notional_usdt)
        elif upper in self._market_driver_notional_contracts():
            base = float(self.settings.market_driver_notional_usdt)
        else:
            base = float(self.settings.regular_alt_notional_usdt)
        mode = getattr(self, "_runtime_mode", str(getattr(self.settings, "trading_mode", "live"))).lower()
        return base * (float(self.settings.test_mode_notional_multiplier) if mode == "test" else 1.0)

    def _market_driver_contracts(self) -> set[str]:
        return {item.strip().upper() for item in str(self.settings.market_driver_contracts).split(",") if item.strip()}

    def _market_driver_notional_contracts(self) -> set[str]:
        return {
            item.strip().upper()
            for item in str(self.settings.market_driver_notional_contracts).split(",")
            if item.strip()
        }

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

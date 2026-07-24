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
        rankings = result.get("rankings", {})
        candidates = list(rankings.get("combined", []))
        # New entries must keep the original combined-ranking gate.  Existing
        # positions, however, must be refreshed from every qualified direction
        # list as well; otherwise a position can disappear from `combined` and
        # miss the scan-time protection/reversal reconciliation.
        sync_candidates = self._unique_signal_candidates(rankings)
        combined_keys = {
            (str(item.get("contract", "")).upper(), str(item.get("direction", "")).lower())
            for item in candidates
            if isinstance(item, dict)
        }
        # A qualified directional list can contain a position's opposite
        # signal even when it did not make the combined top list.  It may
        # reverse an existing position, but it must never create a new one by
        # itself; new entries remain governed by the original 54 ranking.
        reversal_only = []
        for item in sync_candidates:
            if not isinstance(item, dict):
                continue
            key = (str(item.get("contract", "")).upper(), str(item.get("direction", "")).lower())
            if key not in combined_keys and float(item.get("ranking_score", 0) or 0) >= float(self.settings.ranking_min_score):
                reversal_only.append({**item, "_reversal_only": True})
        async with self._order_lock:
            order_candidates = [*candidates, *reversal_only]
            result = await self._process_candidates(order_candidates) if order_candidates else {
                "status": "no_candidates",
                "orders": [],
            }
            try:
                result["position_updates"] = await self._synchronize_positions_from_scan(sync_candidates)
            except Exception as exc:
                logger.exception("scan-time position protection synchronization failed")
                result["position_updates"] = [{
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc) or type(exc).__name__,
                }]
            return result

    @staticmethod
    def _unique_signal_candidates(rankings: dict[str, Any]) -> list[dict[str, Any]]:
        """Return combined/long/short candidates once, preserving rank order.

        The combined list remains the only source used to open new positions.
        This union is only for reconciling already-open positions against the
        latest scan, so a candidate that is not in the combined list can still
        refresh its existing exchange-side protections.
        """
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for bucket in ("combined", "long", "short"):
            values = rankings.get(bucket, [])
            if not isinstance(values, list):
                continue
            for candidate in values:
                if not isinstance(candidate, dict):
                    continue
                contract = str(candidate.get("contract", "")).upper()
                direction = str(candidate.get("direction", "")).lower()
                key = (contract, direction)
                if contract and direction in {"long", "short"} and key not in seen:
                    seen.add(key)
                    result.append(candidate)
        return result

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
        open_orders_by_contract: dict[str, list[dict[str, Any]]] = {}
        for item in open_orders:
            key = str(item.get("contract", "")).upper()
            if key:
                open_orders_by_contract.setdefault(key, []).append(item)
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
            direction = str(candidate.get("direction", "")).lower()
            if direction not in {"long", "short"}:
                actions.append({"contract": contract, "status": "rejected_risk", "code": "INVALID_DIRECTION", "error": "ranking direction must be long or short"})
                continue
            action: dict[str, Any]
            if contract in positions_by_contract:
                current_side = self._position_side(positions_by_contract[contract])
                if current_side == direction:
                    actions.append({"contract": contract, "status": "skipped_existing_position", "direction": direction})
                    continue
                if direction in batch_direction_counts and batch_direction_counts[direction] >= int(self.settings.max_same_direction_orders_per_batch):
                    actions.append({
                        "contract": contract,
                        "status": "skipped_batch_direction_limit",
                        "direction": direction,
                        "limit": int(self.settings.max_same_direction_orders_per_batch),
                    })
                    continue
                # A new Gate scan direction is authoritative.  Cancel old
                # entry/protection orders, close the opposite Bitget position,
                # verify it is gone, then submit the new limit entry.
                info = self._resolve_execution_contract(contract, contracts)
                if info is None:
                    actions.append({
                        "contract": contract,
                        "status": "skipped_contract_unavailable",
                        "code": "BITGET_CONTRACT_UNAVAILABLE",
                        "error": "Gate candidate has no exact active Bitget USDT perpetual match",
                    })
                    continue
                try:
                    await self._close_for_reversal(contract, positions_by_contract[contract], open_orders_by_contract.get(contract, []))
                    action = await self._open_candidate(candidate, info)
                    action["reversed"] = True
                    action["reversal_from"] = current_side
                    actions.append(action)
                    open_contracts.add(contract)
                    open_orders_by_contract[contract] = []
                    positions_by_contract.pop(contract, None)
                    if action.get("status") in {"submitted", "limit_order_open"} and direction in batch_direction_counts:
                        batch_direction_counts[direction] += 1
                except TradingRiskError as exc:
                    actions.append({"contract": contract, "status": "rejected_risk", "code": exc.code, "error": str(exc)})
                except Exception as exc:
                    logger.exception("position reversal failed for %s", contract)
                    actions.append({"contract": contract, "status": "failed", "error_type": type(exc).__name__, "error": str(exc) or type(exc).__name__})
                continue
            if candidate.get("_reversal_only"):
                actions.append(
                    {
                        "contract": contract,
                        "status": "skipped_reversal_only_signal",
                        "direction": direction,
                        "reason": "qualified outside combined ranking; no new entry permitted",
                    }
                )
                continue
            pending_for_contract = open_orders_by_contract.get(contract, [])
            if pending_for_contract:
                pending_sides = {self._order_side(item) for item in pending_for_contract}
                if direction in pending_sides:
                    actions.append({"contract": contract, "status": "skipped_existing_open_order", "direction": direction})
                    continue
                # Replace a stale/opposite pending entry immediately when the
                # 30-minute scan flips direction.  The manager still handles
                # ordinary timeout/momentum cancellation every five seconds.
                try:
                    await self._cancel_pending_orders(contract, pending_for_contract)
                    open_orders_by_contract[contract] = []
                    open_contracts.discard(contract)
                    total_count = max(0, total_count - 1)
                    if contract in driver_contracts:
                        driver_count = max(0, driver_count - 1)
                except TradingRiskError as exc:
                    actions.append({"contract": contract, "status": "rejected_risk", "code": exc.code, "error": str(exc)})
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
    def _position_side(position: dict[str, Any]) -> str:
        return "long" if _number(position.get("size")) > 0 else "short"

    @staticmethod
    def _order_side(order: dict[str, Any]) -> str:
        size = _number(order.get("size"))
        return "long" if size > 0 else "short" if size < 0 else ""

    async def _cancel_pending_orders(self, contract: str, orders: list[dict[str, Any]]) -> None:
        for order in orders:
            order_id = _order_id(order)
            if not order_id:
                continue
            try:
                await self.gate.rest.cancel_futures_order(order_id)
            except Exception as exc:
                raise TradingRiskError(
                    "REVERSE_ENTRY_CANCEL_FAILED",
                    f"could not cancel existing Bitget entry order {order_id}: {type(exc).__name__}: {exc}",
                ) from exc
        remaining = await self.gate.rest.get_open_orders(contract)
        if any(_order_id(item) for item in remaining):
            raise TradingRiskError("REVERSE_ENTRY_CANCEL_FAILED", f"opposite entry order remains open for {contract}")

    async def _close_for_reversal(
        self, contract: str, position: dict[str, Any], pending_orders: list[dict[str, Any]]
    ) -> None:
        try:
            protection_result = await self.gate.rest.cancel_all_price_orders(contract)
            failures = protection_result.get("failureList", []) if isinstance(protection_result, dict) else []
            if failures:
                raise RuntimeError(f"exchange protection cancellation failures: {failures}")
            await self._cancel_pending_orders(contract, pending_orders)
            await self.gate.rest.place_futures_order(
                {
                    "contract": contract,
                    "size": 0,
                    "price": "0",
                    "tif": "ioc",
                    "close": True,
                    "reduce_only": True,
                    "text": f"t-auto-reverse-close-{uuid.uuid4().hex[:12]}",
                    "pos_margin_mode": "cross",
                }
            )
        except TradingRiskError:
            raise
        except Exception as exc:
            raise TradingRiskError(
                "REVERSE_CLOSE_FAILED",
                f"could not close opposite Bitget position for {contract}: {type(exc).__name__}: {exc}",
            ) from exc

        for _ in range(10):
            current = await self.gate.rest.get_position(contract)
            if not current or abs(_number(current.get("size"))) <= 0:
                return
            await asyncio.sleep(0.2)
        raise TradingRiskError("REVERSE_CLOSE_NOT_CONFIRMED", f"Bitget still has an open {contract} position after reversal close")

    async def _synchronize_positions_from_scan(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Refresh active-position protection from the newest qualified scan.

        Gate remains the source of the scan signal and structure levels.  The
        current Bitget position/ticker remains authoritative for size and live
        entry.  A scan may tighten risk or materially revise an unfilled TP,
        but it never loosens an existing stop and never changes a completed TP.
        """
        positions = await self.gate.rest.get_positions()
        active = {
            str(item.get("contract", "")).upper(): item
            for item in positions
            if abs(_number(item.get("size"))) > 0
        }
        if not active:
            return []
        contracts = await self._contracts()
        updates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            contract = str(candidate.get("contract", "")).upper()
            if not contract or contract in seen or contract not in active:
                continue
            seen.add(contract)
            position = active[contract]
            position_side = self._position_side(position)
            if str(candidate.get("direction", "")).lower() != position_side:
                # Opposite directions are handled by _process_candidates.  Do
                # not mutate protection for an old side here.
                continue
            info = contracts.get(contract)
            if info is None:
                continue
            key = f"{contract}:{position_side}"
            record = await self.repository.get_managed_position(key)
            if record is None:
                continue
            try:
                ticker = await self.gate.rest.get_ticker(contract)
                entry = _number(position.get("entry_price"), _number(record.get("plan", {}).get("entry_price")))
                size = abs(_number(position.get("size")))
                if entry <= 0 or size <= 0:
                    continue
                metrics = dict(candidate.get("metrics", {}))
                metrics["ticker"] = {**metrics.get("ticker", {}), **ticker}
                actual_notional = notional_from_size(info, entry, size)
                proposed = build_execution_plan(
                    {**candidate, "metrics": metrics},
                    info,
                    self.settings,
                    entry_price=entry,
                    risk_notional_usdt=actual_notional,
                )
                update = await self._apply_scan_protection_update(
                    record, position, ticker, proposed, info
                )
                record["plan"]["scan_missing_count"] = 0
                record["plan"]["scan_signal_status"] = "same_direction_confirmed"
                record["plan"]["last_scan_seen_at"] = _now().isoformat()
                await self.repository.save_managed_position(record)
                updates.append(update)
            except TradingRiskError as exc:
                updates.append({"contract": contract, "status": "unchanged", "code": exc.code, "error": str(exc)})
            except Exception as exc:
                logger.exception("scan-time protection update failed for %s", contract)
                updates.append({"contract": contract, "status": "failed", "error_type": type(exc).__name__, "error": str(exc) or type(exc).__name__})

        # Strict ranking is intentionally allowed to omit a previously
        # selected coin.  Record that soft deterioration for observability,
        # but do not close the position or loosen its exchange stop solely
        # because it disappeared from this scan.
        for contract, position in active.items():
            if contract in seen:
                continue
            side = self._position_side(position)
            record = await self.repository.get_managed_position(f"{contract}:{side}")
            if record is None:
                continue
            plan = record.get("plan", {})
            missing_count = int(_number(plan.get("scan_missing_count"), 0)) + 1
            plan["scan_missing_count"] = missing_count
            plan["scan_signal_status"] = "not_in_latest_qualified_rankings"
            plan["last_scan_review_at"] = _now().isoformat()
            record["plan"] = plan
            record["updated_at"] = _now().isoformat()
            await self.repository.save_managed_position(record)
            updates.append(
                {
                    "contract": contract,
                    "status": "signal_not_seen",
                    "scan_missing_count": missing_count,
                    "action": "hold_and_review_with_15m_5m_context",
                }
            )
        return updates

    async def _apply_scan_protection_update(
        self, record: dict[str, Any], position: dict[str, Any], ticker: dict[str, Any], proposed: dict[str, Any], info: Any
    ) -> dict[str, Any]:
        plan = record["plan"]
        contract = plan["contract"]
        side = plan["side"]
        price = _number(ticker.get("mark_price")) or _number(ticker.get("last"))
        entry = _number(position.get("entry_price"), _number(plan.get("entry_price")))
        size = abs(_number(position.get("size")))
        atr15 = _number(proposed.get("atr15"), _number(plan.get("atr15")))
        if price <= 0 or entry <= 0 or size <= 0 or atr15 <= 0:
            return {"contract": contract, "status": "unchanged", "reason": "invalid_live_position_data"}

        current_risk = _number(plan.get("initial_risk_distance"))
        signed_move = price - entry if side == "long" else entry - price
        current_r = signed_move / current_risk if current_risk > 0 else 0.0
        stop_threshold = max(0.15 * atr15, entry * 0.0005)
        take_profit_threshold = max(0.2 * atr15, entry * 0.001)
        changed: list[str] = []
        errors: list[str] = []

        proposed_stop = _number(proposed.get("initial_stop"))
        current_stop = _number(plan.get("current_stop"))
        stop_is_live_side = proposed_stop < price if side == "long" else proposed_stop > price
        if (
            stop_is_live_side
            and self._stop_is_better(side, proposed_stop, current_stop)
            and abs(proposed_stop - current_stop) >= stop_threshold
        ):
            old_id = plan.get("protection_order_ids", {}).get("stop")
            try:
                new_id = await self._replace_trigger(
                    plan, "stop", proposed_stop, "0", record["position_key"], old_id
                )
                plan["protection_order_ids"]["stop"] = new_id
                plan["current_stop"] = proposed_stop
                plan["last_stop_update"] = _now().isoformat()
                changed.append("stop")
            except Exception as exc:
                errors.append(f"stop:{type(exc).__name__}: {exc}")

        completed = set(plan.get("completed_stages", []))
        proposed_targets = {item["stage"]: item for item in proposed.get("take_profits", [])}
        for target in plan.get("take_profits", []):
            stage = target["stage"]
            if stage in completed or stage not in proposed_targets:
                continue
            new_price = _number(proposed_targets[stage].get("price"))
            old_price = _number(target.get("price"))
            if new_price <= 0 or old_price <= 0:
                continue
            new_is_live_side = new_price > price if side == "long" else new_price < price
            moving_further = new_price > old_price if side == "long" else new_price < old_price
            moving_closer_for_profit = current_r >= 1.0 and (
                new_price < old_price if side == "long" else new_price > old_price
            )
            if not new_is_live_side or not (moving_further or moving_closer_for_profit):
                continue
            if abs(new_price - old_price) < take_profit_threshold:
                continue
            old_id = plan.get("protection_order_ids", {}).get(stage)
            try:
                new_id = await self._replace_trigger(
                    plan,
                    stage,
                    new_price,
                    partial_close_size(side, str(size), target["percent"]),
                    record["position_key"],
                    old_id,
                )
                target["price"] = new_price
                target["rr"] = abs(new_price - entry) / current_risk if current_risk > 0 else target.get("rr", 0)
                target["source"] = "scan_refresh"
                plan["protection_order_ids"][stage] = new_id
                changed.append(stage)
            except Exception as exc:
                errors.append(f"{stage}:{type(exc).__name__}: {exc}")

        plan["market_state"] = proposed.get("market_state", plan.get("market_state"))
        plan["risk_flags"] = list(proposed.get("risk_flags", plan.get("risk_flags", [])))
        plan["ranking_score"] = proposed.get("ranking_score", plan.get("ranking_score"))
        plan["atr15"] = proposed.get("atr15", plan.get("atr15"))
        plan["atr5"] = proposed.get("atr5", plan.get("atr5"))
        record["plan"] = plan
        record["current_size"] = size
        record["updated_at"] = _now().isoformat()
        if errors:
            record["protection_status"] = "backend_fallback"
            record["protection_error"] = "; ".join(errors)
        elif changed:
            record["protection_status"] = "exchange"
            record.pop("protection_error", None)
        await self.repository.save_managed_position(record)
        return {
            "contract": contract,
            "status": "updated" if changed and not errors else "partial" if changed else "unchanged",
            "changed": changed,
            "current_r": current_r,
            "protection_status": record.get("protection_status", "exchange"),
            "errors": errors,
        }

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

    async def _confirm_entry_order(
        self, contract: str, order_id: str, client_id: str, attempts: int = 6
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        """Reconcile a Bitget limit-order response before reporting success.

        Bitget acknowledges an order before the pending-order endpoint is
        necessarily updated.  Conversely, a limit order can fill or be
        cancelled during that short window.  Reading position, pending orders
        and (when supported) the order-detail endpoint prevents the service
        from silently treating a missing order as a successful entry.
        """
        latest_position: dict[str, Any] | None = None
        latest_order: dict[str, Any] | None = None
        latest_detail: dict[str, Any] | None = None
        for attempt in range(max(1, attempts)):
            try:
                latest_position = await self.gate.rest.get_position(contract)
            except Exception as exc:
                logger.debug("Bitget position read delayed for %s: %s", contract, type(exc).__name__)
            if latest_position and abs(_number(latest_position.get("size"))) > 0:
                return latest_position, latest_order, latest_detail
            try:
                open_orders = await self.gate.rest.get_open_orders(contract)
                latest_order = next(
                    (
                        item
                        for item in open_orders
                        if str(_order_id(item) or "") == str(order_id)
                        or str(item.get("text") or item.get("clientOid") or "") == str(client_id)
                    ),
                    None,
                )
            except Exception as exc:
                logger.debug("Bitget pending-order read delayed for %s: %s", contract, type(exc).__name__)
            if latest_order is not None:
                return latest_position, latest_order, latest_detail
            detail_reader = getattr(self.gate.rest, "get_order_detail", None)
            if detail_reader is not None:
                try:
                    latest_detail = await detail_reader(order_id=order_id, client_oid=client_id, contract=contract)
                except Exception as exc:
                    logger.debug("Bitget order detail unavailable for %s: %s", order_id, type(exc).__name__)
                state = str((latest_detail or {}).get("state") or "").lower()
                if state in {"live", "new", "partially_filled", "partial-fill", "partial_filled"}:
                    return latest_position, latest_order or latest_detail, latest_detail
                if state in {"canceled", "cancelled", "rejected", "expired", "failed", "fail"}:
                    return latest_position, latest_order, latest_detail
            if attempt + 1 < attempts:
                await asyncio.sleep(0.2 * (attempt + 1))
        return latest_position, latest_order, latest_detail

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
        notional = self._notional(info.name, candidate.get("contract_type"))
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
            position, confirmed_order, order_detail = await self._confirm_entry_order(
                info.name, entry_order_id, client_id
            )
        except Exception as exc:
            logger.warning("entry confirmation delayed after limit order %s: %s", entry_order_id, type(exc).__name__)
            position, confirmed_order, order_detail = None, None, None
        actual_size = abs(_number((position or {}).get("size")))
        if actual_size <= 0 and confirmed_order is None:
            raise TradingRiskError(
                "ENTRY_ORDER_NOT_CONFIRMED",
                f"Bitget accepted {entry_order_id} but neither an open limit order nor a position was confirmed",
            )
        detail_state = str((order_detail or confirmed_order or {}).get("state") or "").lower()
        if actual_size <= 0 and detail_state in {"canceled", "cancelled", "rejected", "expired", "failed", "fail"}:
            raise TradingRiskError(
                "ENTRY_ORDER_NOT_ACTIVE",
                f"Bitget entry {entry_order_id} is {detail_state}, so no position was opened",
            )
        if actual_size <= 0 and detail_state in {"filled", "full_fill", "full-filled"}:
            raise TradingRiskError(
                "ENTRY_FILL_NOT_CONFIRMED",
                f"Bitget reports entry {entry_order_id} filled but the position read-back is still empty",
            )
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
                    "order_confirmation": confirmed_order or order_detail,
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
                "entry_confirmation": confirmed_order or order_detail,
            }
            await self._notify_order(action)
            return action
        try:
            await self.gate.rest.cancel_futures_order(entry_order_id)
        except Exception:
            logger.info("entry order %s already filled or cancelled", entry_order_id)
        try:
            remaining_entry = await self.gate.rest.get_open_orders(info.name)
        except Exception as exc:
            await self._emergency_close(info.name, f"{info.name}:{side}")
            raise TradingRiskError(
                "ENTRY_REMAINDER_UNCERTAIN",
                f"could not verify cancellation of filled Bitget entry {entry_order_id}: {type(exc).__name__}: {exc}",
            ) from exc
        if any(str(_order_id(item) or "") == str(entry_order_id) for item in remaining_entry):
            await self._emergency_close(info.name, f"{info.name}:{side}")
            raise TradingRiskError(
                "ENTRY_REMAINDER_CANCEL_FAILED",
                f"Bitget left a live remainder of filled entry order {entry_order_id} on {info.name}",
            )
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
        plan["initial_position_size"] = entry_size
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

    async def _wait_for_price_order(
        self, contract: str, order_id: str, present: bool, attempts: int = 5
    ) -> bool:
        for attempt in range(max(1, attempts)):
            open_orders = await self.gate.rest.get_price_orders(status="open", contract=contract)
            visible = {_order_id(item) for item in open_orders}
            if (order_id in visible) is present:
                return True
            if attempt + 1 < attempts:
                await asyncio.sleep(0.2 * (attempt + 1))
        return False

    async def _replace_trigger(
        self,
        plan: dict[str, Any],
        kind: str,
        trigger_price: float,
        size: str,
        position_key: str,
        old_id: str | None,
    ) -> str:
        """Atomically replace one exchange trigger from the bot's perspective.

        The new trigger is created and read back first.  The old trigger is
        cancelled only after that confirmation, then its absence is verified.
        If either side cannot be confirmed, the method raises and the caller
        keeps the persisted old plan rather than claiming a successful move.
        """
        try:
            new_id = await self._create_trigger(plan, kind, trigger_price, size, position_key)
            if not await self._wait_for_price_order(plan["contract"], new_id, True):
                raise RuntimeError(f"new {kind} protection order {new_id} was not visible")
        except Exception as exc:
            raise TradingRiskError(
                "PROTECTION_REPLACEMENT_FAILED",
                f"new {kind} protection could not be confirmed for {plan['contract']}: {type(exc).__name__}: {exc}",
            ) from exc

        if old_id and str(old_id) != str(new_id):
            try:
                await self.gate.rest.cancel_price_order(old_id)
                if not await self._wait_for_price_order(plan["contract"], str(old_id), False):
                    raise RuntimeError(f"old {kind} protection order {old_id} remains open")
            except Exception as exc:
                try:
                    await self.gate.rest.cancel_price_order(new_id)
                except Exception:
                    logger.exception("failed to roll back replacement %s for %s", new_id, plan["contract"])
                raise TradingRiskError(
                    "PROTECTION_REPLACEMENT_FAILED",
                    f"old {kind} protection could not be removed for {plan['contract']}: {type(exc).__name__}: {exc}",
                ) from exc
        return new_id

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

    async def _close_for_trend_break(self, contract: str, position: dict[str, Any]) -> None:
        """Flatten a clearly invalid losing setup after exchange cleanup."""
        result = await self.gate.rest.cancel_all_price_orders(contract)
        failures = result.get("failureList", []) if isinstance(result, dict) else []
        if failures:
            raise TradingRiskError(
                "TREND_BREAK_PROTECTION_CANCEL_FAILED",
                f"could not cancel Bitget protections before trend-break close: {failures}",
            )
        await self.gate.rest.place_futures_order(
            {
                "contract": contract,
                "size": 0,
                "price": "0",
                "tif": "ioc",
                "close": True,
                "reduce_only": True,
                "text": f"t-auto-trend-break-{uuid.uuid4().hex[:12]}",
                "pos_margin_mode": "cross",
            }
        )
        for _ in range(10):
            current = await self.gate.rest.get_position(contract)
            if not current or abs(_number(current.get("size"))) <= 0:
                return
            await asyncio.sleep(0.2)
        raise TradingRiskError("TREND_BREAK_CLOSE_NOT_CONFIRMED", f"Bitget still has an open {contract} position")

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
                cancel_response = await self.gate.rest.cancel_futures_order(order_id)
                # A cancel response alone is not enough: Bitget can race a
                # fill, and some adapters return success for an already gone
                # order.  Read the exchange book again before reporting the
                # order as cancelled.
                remaining = await self.gate.rest.get_open_orders(contract)
                if any(_order_id(item) == order_id for item in remaining):
                    actions.append({
                        "contract": contract,
                        "status": "limit_order_cancel_failed",
                        "code": "LIMIT_ENTRY_CANCEL_NOT_CONFIRMED",
                        "side": side,
                        "entry_order_id": order_id,
                        "entry_limit_price": limit_price,
                        "age_seconds": round(age, 1),
                    })
                    continue
                action = {
                    "contract": contract,
                    "status": "limit_order_cancelled",
                    "code": reason,
                    "side": side,
                    "entry_order_id": order_id,
                    "entry_limit_price": limit_price,
                    "age_seconds": round(age, 1),
                    "cancel_response": cancel_response,
                    "cancel_confirmed": True,
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
                    await self._clear_exchange_protection(contract)
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

    async def _clear_exchange_protection(self, contract: str) -> None:
        """Remove orphaned bot trigger orders before adopting a position.

        This is used only when the database has no managed record.  Without
        the saved IDs there is no safe way to know which old stop/TP belongs
        to the current plan, so a clean exchange-side set is installed.
        """
        existing = await self.gate.rest.get_price_orders(status="open", contract=contract)
        if not existing:
            return
        result = await self.gate.rest.cancel_all_price_orders(contract)
        failures = result.get("failureList", []) if isinstance(result, dict) else []
        if failures:
            raise TradingRiskError(
                "PROTECTION_CLEANUP_FAILED",
                f"could not remove orphaned protection for {contract}: {failures}",
            )
        if await self.gate.rest.get_price_orders(status="open", contract=contract):
            raise TradingRiskError("PROTECTION_CLEANUP_FAILED", f"orphaned protection remains open for {contract}")

    async def _manage_position(
        self, record: dict[str, Any], position: dict[str, Any], ticker: dict[str, Any], context: dict[str, Any], info: Any
    ) -> dict[str, Any]:
        plan = record["plan"]
        plan["margin_mode"] = self._position_margin_mode(position) or plan.get(
            "margin_mode", "cross"
        )
        price = _number(ticker.get("mark_price")) or _number(ticker.get("last"))
        entry = _number(position.get("entry_price"), _number(plan.get("entry_price")))
        live_size = abs(_number(position.get("size")))
        protection_error = None
        try:
            missing_protection = await self._ensure_protection(plan, live_size, record["position_key"])
        except Exception as exc:
            missing_protection = {"stop", *[target["stage"] for target in plan.get("take_profits", [])]}
            protection_error = f"{type(exc).__name__}: {exc}"
            logger.exception("exchange protection refresh failed for %s; backend fallback enabled", plan["contract"])

        all_protection_stages = {
            "stop",
            *[
                target["stage"]
                for target in plan.get("take_profits", [])
                if target["stage"] not in plan.get("completed_stages", [])
            ],
        }
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
        trend_break_score = self._trend_break_score(plan, context)
        plan["trend_break_score"] = trend_break_score
        plan["trend_checked_at"] = _now().isoformat()

        # Do not close merely because the strict 30m ranking omitted a symbol.
        # Close a losing position early only when the faster exchange-market
        # context confirms a multi-factor structure failure.
        if trend_break_score >= 3 and current_r < 0:
            try:
                await self._close_for_trend_break(plan["contract"], position)
                record["status"] = "closed"
                record["closed_at"] = _now().isoformat()
                record["plan"] = plan
                record["updated_at"] = _now().isoformat()
                await self.repository.save_managed_position(record)
                action = {
                    "contract": plan["contract"],
                    "status": "trend_break_closed",
                    "reason": "15m/5m structure and EMA trend failed",
                    "trend_break_score": trend_break_score,
                    "current_r": current_r,
                    "protection_status": "exchange_cleanup_before_close",
                }
                await self.repository.save_order_event(
                    {
                        "event_id": uuid.uuid4().hex,
                        "client_order_id": None,
                        "contract": plan["contract"],
                        "event_type": action["status"],
                        "created_at": _now(),
                        "payload": action,
                    }
                )
                await self._notify_order(action)
                return action
            except Exception as exc:
                protection_error = protection_error or f"trend_break_close:{type(exc).__name__}: {exc}"
                logger.exception("confirmed trend-break close failed for %s", plan["contract"])
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
                new_id = await self._replace_trigger(
                    plan, "stop", candidate_stop, "0", record["position_key"], old_id
                )
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

        try:
            weakness_tp_moved = await self._tighten_take_profit_for_weakness(
                record, plan, price, context, trend_break_score
            )
            tp_moved = bool(tp_moved or weakness_tp_moved)
        except Exception as exc:
            protection_error = protection_error or f"trend_tp:{type(exc).__name__}: {exc}"
            logger.exception("exchange weakness take-profit update failed for %s", plan["contract"])

        if protection_error:
            record["protection_status"] = "backend_fallback"
            record["protection_error"] = protection_error
        repaired = list(plan.pop("last_protection_repair_stages", []) or [])
        record["plan"] = plan
        record["current_size"] = live_size
        record["updated_at"] = _now().isoformat()
        await self.repository.save_managed_position(record)
        if repaired or stop_moved or tp_moved or fallback_actions:
            await self.repository.save_order_event(
                {
                    "event_id": uuid.uuid4().hex,
                    "client_order_id": None,
                    "contract": record["contract"],
                    "event_type": "protection_reconciled",
                    "created_at": _now(),
                    "payload": {
                        "contract": record["contract"],
                        "repaired_stages": repaired,
                        "stop_changed": stop_moved,
                        "take_profit_changed": tp_moved,
                        "fallback_actions": fallback_actions,
                        "trend_break_score": trend_break_score,
                    },
                }
            )
        result = {
            "contract": record["contract"],
            "status": "managed",
            "phase": plan["phase"],
            "current_r": current_r,
            "stop_changed": stop_moved,
            "take_profit_changed": tp_moved,
            "protection_repaired": repaired,
            "trend_break_score": trend_break_score,
            "current_stop": plan.get("current_stop"),
            "protection_status": record.get("protection_status", "exchange"),
            "fallback_actions": fallback_actions,
        }
        if record.get("protection_error"):
            result["protection_error"] = record["protection_error"]
        return result

    @staticmethod
    def _protection_stage_from_order(order: dict[str, Any]) -> str | None:
        """Identify only this application's trigger orders.

        Manual Bitget triggers must never be cancelled as if they belonged to
        the bot.  The deterministic clientOid created by ``_create_trigger``
        is the ownership marker used for orphan cleanup.
        """
        text = str(order.get("text") or order.get("clientOid") or "").lower()
        if not text.startswith("t-auto-"):
            return None
        if "-sl-" in text:
            return "stop"
        for stage in ("TP1", "TP2", "TP3"):
            if f"-{stage.lower()}-" in text:
                return stage
        return None

    @staticmethod
    def _infer_completed_take_profits(plan: dict[str, Any], live_size: float) -> list[str]:
        """Infer exchange-filled partial TPs from the remaining position size.

        A filled Bitget trigger disappears from the pending list.  Recreating
        that same TP on the next five-second cycle would create a duplicate
        exit.  The original entry size and configured stage percentages give a
        conservative, exchange-independent way to recognize a partial fill.
        """
        initial_size = _number(plan.get("initial_position_size")) or _number(plan.get("entry_size"))
        if initial_size <= 0 or live_size <= 0 or live_size >= initial_size:
            return []
        tolerance = max(initial_size * 0.01, 1e-12)
        cumulative = 0.0
        completed = set(plan.get("completed_stages", []))
        inferred: list[str] = []
        for target in plan.get("take_profits", []):
            stage = str(target.get("stage", ""))
            if not stage or stage in completed:
                cumulative += _number(target.get("percent"))
                continue
            cumulative += _number(target.get("percent"))
            expected_remaining = initial_size * max(0.0, 1.0 - cumulative)
            if live_size <= expected_remaining + tolerance:
                inferred.append(stage)
            else:
                break
        return inferred

    async def _cancel_protection_order(self, order_id: str, contract: str) -> None:
        await self.gate.rest.cancel_price_order(order_id)
        if not await self._wait_for_price_order(contract, order_id, False):
            raise TradingRiskError(
                "PROTECTION_CANCEL_NOT_CONFIRMED",
                f"Bitget protection order {order_id} remains open on {contract}",
            )

    async def _ensure_protection(self, plan: dict[str, Any], entry_size: float, position_key: str) -> set[str]:
        """Reconcile the complete exchange-side protection set.

        Every replacement is create -> verify -> cancel old -> verify absent.
        Missing IDs, stale bot clientOids and TPs that already filled are all
        reconciled here.  Returning an empty set means the exchange set is
        complete; callers only use the fallback closer when this method raises.
        """
        contract = plan["contract"]
        plan["entry_size"] = entry_size
        plan.setdefault("initial_position_size", entry_size)
        open_orders = await self.gate.rest.get_price_orders(status="open", contract=contract)
        open_ids = {_order_id(item) for item in open_orders if _order_id(item)}
        ids = plan.setdefault("protection_order_ids", {})

        inferred = self._infer_completed_take_profits(plan, entry_size)
        if inferred:
            completed = list(dict.fromkeys([*plan.get("completed_stages", []), *inferred]))
            plan["completed_stages"] = completed

        desired: dict[str, tuple[float, str]] = {"stop": (_number(plan.get("current_stop")), "0")}
        for target in plan.get("take_profits", []):
            stage = str(target.get("stage", ""))
            if stage and stage not in plan.get("completed_stages", []):
                desired[stage] = (
                    _number(target.get("price")),
                    partial_close_size(plan["side"], str(entry_size), target["percent"]),
                )

        # A TP which has already filled must not be recreated.  Remove a
        # lingering duplicate for that completed stage first.
        for order in open_orders:
            orphan_stage = self._protection_stage_from_order(order)
            order_id = _order_id(order)
            if orphan_stage and orphan_stage not in desired and order_id:
                await self._cancel_protection_order(order_id, contract)
                open_ids.discard(order_id)

        repaired: list[str] = []
        for stage, (price, size) in desired.items():
            if price <= 0:
                raise TradingRiskError("INVALID_PROTECTION_PRICE", f"invalid {stage} protection price for {contract}")
            current_id = str(ids.get(stage)) if ids.get(stage) not in (None, "") else None
            current_order = next(
                (item for item in open_orders if _order_id(item) == current_id),
                None,
            )
            stop_size_changed = False
            if stage == "stop" and current_order is not None:
                order_size = _number((current_order.get("initial") or {}).get("size"))
                if order_size > 0:
                    stop_size_changed = abs(order_size - entry_size) > max(entry_size * 0.005, 1e-12)
            if current_id in open_ids and not stop_size_changed:
                for order in open_orders:
                    duplicate_stage = self._protection_stage_from_order(order)
                    duplicate_id = _order_id(order)
                    if duplicate_stage == stage and duplicate_id and duplicate_id != current_id:
                        await self._cancel_protection_order(duplicate_id, contract)
                        open_ids.discard(duplicate_id)
                continue

            # If the database ID is stale, create the new order first.  Any
            # old bot order for the same stage is cancelled only afterwards.
            new_id = await self._replace_trigger(
                plan,
                stage,
                price,
                size,
                position_key,
                current_id if current_id in open_ids else None,
            )
            ids[stage] = new_id
            open_ids.add(new_id)
            repaired.append(stage)

            for order in open_orders:
                orphan_stage = self._protection_stage_from_order(order)
                orphan_id = _order_id(order)
                if orphan_stage == stage and orphan_id and orphan_id not in {current_id, new_id}:
                    await self._cancel_protection_order(orphan_id, contract)
                    open_ids.discard(orphan_id)

        await self._verify_exchange_protection(plan)
        if repaired:
            plan["last_protection_repair"] = _now().isoformat()
            plan["last_protection_repair_stages"] = repaired
        else:
            plan.pop("last_protection_repair_stages", None)
        return set()

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
                    pending_actions = await self._monitor_pending_entries()
                    for action in pending_actions:
                        # Pending entries are intentionally not positions yet,
                        # but their submission/cancellation lifecycle must be
                        # visible in the order channel so a user can tell the
                        # difference between "not submitted" and "not filled".
                        await self.repository.save_order_event(
                            {
                                "event_id": uuid.uuid4().hex,
                                "client_order_id": action.get("entry_order_id"),
                                "contract": action.get("contract"),
                                "event_type": action.get("status"),
                                "created_at": _now(),
                                "payload": action,
                            }
                        )
                        await self._notify_order(action)
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
        old_id = plan.get("protection_order_ids", {}).get(target["stage"])
        new_id = await self._replace_trigger(
            plan,
            target["stage"],
            new_price,
            partial_close_size(plan["side"], str(record.get("current_size", 0)), target["percent"]),
            record["position_key"],
            old_id,
        )
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
            context[f"last_close{label}"] = float(close.iloc[-1])
            context[f"ema20{label}"] = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
            context[f"ema50{label}"] = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        self._market_cache[contract] = (time.monotonic(), context)
        return ticker, context

    @staticmethod
    def _trend_break_score(plan: dict[str, Any], context: dict[str, Any]) -> int:
        """Score a confirmed local structure failure without using scan absence.

        The 30m/4h Gate scan is the entry authority.  This faster 15m/5m
        guard is only for an already-open position and needs multiple aligned
        failures, so one strict scan that omits a symbol cannot close it.
        """
        side = str(plan.get("side", "")).lower()
        last15 = _number(context.get("last_close15"))
        ema20_15 = _number(context.get("ema2015"))
        ema50_15 = _number(context.get("ema5015"))
        last5 = _number(context.get("last_close5"))
        ema20_5 = _number(context.get("ema205"))
        recent_low15 = _number(context.get("recent_low15"))
        recent_high15 = _number(context.get("recent_high15"))
        if side == "long":
            checks = (
                last15 > 0 and ema20_15 > 0 and ema50_15 > 0 and last15 < ema20_15 < ema50_15,
                last5 > 0 and ema20_5 > 0 and last5 < ema20_5,
                last15 > 0 and recent_low15 > 0 and last15 < recent_low15,
                last5 > 0 and _number(context.get("recent_low5")) > 0 and last5 < _number(context.get("recent_low5")),
            )
        elif side == "short":
            checks = (
                last15 > 0 and ema20_15 > 0 and ema50_15 > 0 and last15 > ema20_15 > ema50_15,
                last5 > 0 and ema20_5 > 0 and last5 > ema20_5,
                last15 > 0 and recent_high15 > 0 and last15 > recent_high15,
                last5 > 0 and _number(context.get("recent_high5")) > 0 and last5 > _number(context.get("recent_high5")),
            )
        else:
            return 0
        return sum(bool(item) for item in checks)

    async def _tighten_take_profit_for_weakness(
        self, record: dict[str, Any], plan: dict[str, Any], price: float, context: dict[str, Any], trend_score: int
    ) -> bool:
        """Move the next unfinished TP closer only after profitable weakness."""
        if trend_score < 2 or _number(plan.get("current_r_multiple")) < 1.0:
            return False
        last_update = plan.get("last_take_profit_update")
        if last_update:
            try:
                if (_now() - datetime.fromisoformat(last_update)).total_seconds() < 300:
                    return False
            except ValueError:
                pass
        atr15 = _number(context.get("atr15"), _number(plan.get("atr15")))
        if atr15 <= 0 or price <= 0:
            return False
        target = next(
            (item for item in plan.get("take_profits", []) if item["stage"] not in plan.get("completed_stages", [])),
            None,
        )
        if target is None:
            return False
        old_price = _number(target.get("price"))
        new_price = price + 0.5 * atr15 if plan["side"] == "long" else price - 0.5 * atr15
        live_side = new_price > price if plan["side"] == "long" else new_price < price
        closer = new_price < old_price if plan["side"] == "long" else new_price > old_price
        if not live_side or not closer or abs(new_price - old_price) < 0.15 * atr15:
            return False
        new_id = await self._replace_trigger(
            plan,
            target["stage"],
            new_price,
            partial_close_size(plan["side"], str(record.get("current_size", 0)), target["percent"]),
            record["position_key"],
            plan.get("protection_order_ids", {}).get(target["stage"]),
        )
        plan.setdefault("protection_order_ids", {})[target["stage"]] = new_id
        target["price"] = new_price
        target["source"] = "trend_weakness_guard"
        plan["last_take_profit_update"] = _now().isoformat()
        return True

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
            and str((getattr(info, "raw", {}) or {}).get("symbolType", "perpetual")).lower()
            in {"", "perpetual"}
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _normalize_contract(raw: dict[str, Any]) -> Any:
        from app.gate.normalizer import normalize_contract

        return normalize_contract(raw)

    def _notional(self, contract: str, contract_type: str | None = None) -> float:
        upper = contract.upper()
        category = str(contract_type or "").strip().lower()
        if category in {"stock", "stocks", "equity", "equities"}:
            base = float(self.settings.stock_notional_usdt)
        elif upper in {"BTC_USDT", "ETH_USDT"}:
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

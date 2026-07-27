import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from math import isfinite
from typing import Any

import pandas as pd

from app.gate.normalizer import closed_candles, normalize_candles
from app.indicators.atr import atr
from app.indicators.volatility_regime import adaptive_volatility_profile
from app.trading.risk import (
    TradingRiskError,
    adaptive_operational_leverage,
    build_execution_plan,
    dynamic_position_notional,
    managed_stop_candidate,
    max_leverage_for_notional,
    notional_from_size,
    notional_for_contract,
    planned_take_profit_sizes,
    signed_size,
)

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if isfinite(result) else default


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
        # Keep momentum cancellation state between management ticks.  A
        # single noisy ticker sample must not cancel a valid passive entry.
        self._pending_momentum_observations: dict[str, dict[str, Any]] = {}
        # This is populated at submission time with the signal's shared
        # 72h/6h/2h adaptive volatility scale. It lets volatile contracts
        # receive a wider, still capped, guard while keeping a safe global
        # fallback after a process restart.
        self._pending_entry_metadata: dict[str, dict[str, Any]] = {}

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
        candidates = list(result.get("rankings", {}).get("combined", []))
        if not self.enabled:
            actions = [
                {"contract": item.get("contract"), "side": item.get("direction"), "status": "skipped_trading_disabled", "reason": "AUTO_ORDER_ENABLED is false"}
                for item in candidates
            ]
            await self._notify_decisions(actions)
            return {"status": "disabled", "orders": actions}
        control = await self.repository.get_trading_control()
        self._runtime_mode = str(control.get("mode") or self.settings.trading_mode).lower()
        if control.get("paused"):
            actions = [
                {"contract": item.get("contract"), "side": item.get("direction"), "status": "skipped_trading_paused", "reason": control.get("reason")}
                for item in candidates
            ]
            await self._notify_decisions(actions)
            return {"status": "paused", "reason": control.get("reason"), "orders": actions}
        rankings = result.get("rankings", {})
        scan_analysis = result.get("_scan_analysis")
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
                result["position_updates"] = await self._synchronize_positions_from_scan(
                    sync_candidates,
                    scan_analysis,
                )
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
                    await self._reset_reversal_confirmation(contract, current_side)
                    actions.append({"contract": contract, "status": "skipped_existing_position", "direction": direction})
                    continue
                # An opposite ranking must persist across distinct completed
                # 30m candles before a full position flip. A hard 30m
                # structural failure remains eligible for immediate exit in
                # the manager; this confirmation only prevents flip-flopping.
                reversal = await self._confirm_reversal_signal(
                    contract,
                    current_side,
                    direction,
                    candidate,
                )
                if not reversal["confirmed"]:
                    actions.append(
                        {
                            "contract": contract,
                            "status": "reversal_confirmation_pending",
                            "direction": direction,
                            "reversal_from": current_side,
                            "reason": "opposite 30m ranking needs persistence before position flip",
                            "confirmations": reversal["confirmations"],
                            "required_confirmations": reversal["required"],
                            "observation": reversal["observation"],
                        }
                    )
                    continue
                # Confirmed opposite scan: cancel old entry/protection orders,
                # close the position, verify it is gone, then open the new side.
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
                except TradingRiskError as exc:
                    actions.append(self._trading_error_action(contract, exc))
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
                    actions.append(self._trading_error_action(contract, exc))
                    continue
            prior = await self.repository.get_managed_position(f"{contract}:{direction}")
            if prior and str(prior.get("status", "")).lower() == "closed":
                closed_at = self._as_utc_datetime(prior.get("closed_at"))
                if closed_at is not None:
                    elapsed_minutes = (_now() - closed_at).total_seconds() / 60
                    cooldown = float(self.settings.reentry_cooldown_minutes)
                    if elapsed_minutes < cooldown:
                        actions.append(
                            {
                                "contract": contract,
                                "status": "skipped_reentry_cooldown",
                                "direction": direction,
                                "code": "REENTRY_COOLDOWN",
                                "reason": "recent exit; waiting for a fresh market cycle instead of fee churn",
                                "cooldown_remaining_minutes": round(cooldown - elapsed_minutes, 1),
                            }
                        )
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
            except TradingRiskError as exc:
                action = self._trading_error_action(contract, exc)
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
        candidate_by_contract = {
            str(item.get("contract", "")).upper(): item
            for item in candidates
            if isinstance(item, dict)
        }
        for action in actions:
            candidate = candidate_by_contract.get(str(action.get("contract", "")).upper(), {})
            action.setdefault("side", action.get("direction") or candidate.get("direction"))
            for key in ("ranking_score", "confidence", "market_state", "signal_state"):
                if key in candidate:
                    action.setdefault(key, candidate[key])
        await self._notify_decisions(actions)
        return {"status": "completed", "orders": actions}

    async def _reset_reversal_confirmation(self, contract: str, side: str) -> None:
        record = await self.repository.get_managed_position(f"{contract}:{side}")
        if not record:
            return
        plan = record.get("plan", {})
        keys = (
            "reversal_candidate_direction",
            "reversal_candidate_observation",
            "reversal_candidate_confirmations",
        )
        changed = any(key in plan for key in keys)
        for key in keys:
            plan.pop(key, None)
        if changed:
            record["plan"] = plan
            record["updated_at"] = _now().isoformat()
            await self.repository.save_managed_position(record)

    async def _confirm_reversal_signal(
        self,
        contract: str,
        current_side: str,
        opposite_side: str,
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        """Confirm a reversal with independent closed 30m observations."""
        required = max(
            2,
            int(getattr(self.settings, "reversal_signal_confirmations", 2)),
        )
        metrics = candidate.get("metrics", {})
        frame30 = metrics.get("30m", {}) if isinstance(metrics, dict) else {}
        observation = str(
            frame30.get("closed_timestamp")
            if isinstance(frame30, dict)
            else ""
        )
        if not observation:
            observation = str(int(_now().timestamp()) // (30 * 60))
        record = await self.repository.get_managed_position(
            f"{contract}:{current_side}"
        )
        if not record:
            return {
                "confirmed": False,
                "confirmations": 0,
                "required": required,
                "observation": observation,
            }
        plan = record.get("plan", {})
        prior_direction = str(plan.get("reversal_candidate_direction") or "")
        prior_observation = str(plan.get("reversal_candidate_observation") or "")
        confirmations = int(
            _number(plan.get("reversal_candidate_confirmations"), 0)
        )
        if opposite_side != prior_direction:
            confirmations = 0
        if observation != prior_observation:
            confirmations += 1
        plan["reversal_candidate_direction"] = opposite_side
        plan["reversal_candidate_observation"] = observation
        plan["reversal_candidate_confirmations"] = confirmations
        record["plan"] = plan
        record["updated_at"] = _now().isoformat()
        await self.repository.save_managed_position(record)
        return {
            "confirmed": confirmations >= required,
            "confirmations": confirmations,
            "required": required,
            "observation": observation,
        }

    @staticmethod
    def _trading_error_action(
        contract: str,
        error: TradingRiskError,
    ) -> dict[str, Any]:
        """Separate strategy rejection from exchange/preflight failures."""
        exchange_preflight_codes = {
            "CROSS_MARGIN_NOT_CONFIRMED",
            "POSITION_MODE_NOT_CONFIRMED",
            "MAX_LEVERAGE_UNAVAILABLE",
            "ENTRY_ORDER_ID_MISSING",
            "ENTRY_ORDER_NOT_CONFIRMED",
            "ENTRY_ORDER_NOT_ACTIVE",
            "ENTRY_FILL_NOT_CONFIRMED",
        }
        exchange_constraint_codes = {
            "ORDER_SIZE_TOO_SMALL",
            "ORDER_SIZE_ZERO",
            "ORDER_NOTIONAL_TOO_SMALL",
            "LEVERAGE_SAFETY_UNAVAILABLE",
            "EXCHANGE_OPEN_CAPACITY_ZERO",
            "EXCHANGE_CAPACITY_BELOW_VIABLE_POSITION",
        }
        if error.code in exchange_preflight_codes:
            status = "failed_exchange_preflight"
            reason = "Bitget account/order preflight could not be confirmed"
        elif error.code in exchange_constraint_codes:
            status = "skipped_exchange_constraint"
            reason = "Bitget contract limits cannot safely accept this calculated order"
        else:
            status = "rejected_risk"
            reason = "trade plan did not pass strategy risk validation"
        return {
            "contract": contract,
            "status": status,
            "code": error.code,
            "reason": reason,
            "error": str(error),
        }

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
            if not await self._wait_for_no_price_orders(contract):
                raise RuntimeError(f"exchange protection orders remain open for {contract}")
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

    async def _synchronize_positions_from_scan(
        self,
        candidates: list[dict[str, Any]],
        scan_analysis: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Recalculate every open position on each 30m Gate scan.

        The combined/long/short rankings remain the entry and reversal
        authority.  The scanner also hands this method every successfully
        analysed Gate contract so a held coin can refresh its structure-based
        protection even after falling below the qualified top-N lists.  If a
        contract was excluded before analysis, the existing position falls
        back to the faster Bitget 15m/5m context; it is never closed merely
        because ranking omitted it.
        """
        positions = await self.gate.rest.get_positions()
        active = {
            str(item.get("contract", "")).upper(): item
            for item in positions
            if abs(_number(item.get("size"))) > 0
        }
        if not active:
            return []
        try:
            contracts = await self._contracts()
        except Exception as exc:
            logger.warning("Bitget contract metadata unavailable during scan refresh: %s", type(exc).__name__)
            contracts = {}

        qualified_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        for rank_candidate in candidates:
            if not isinstance(rank_candidate, dict):
                continue
            contract = str(rank_candidate.get("contract", "")).upper()
            side = str(rank_candidate.get("direction", "")).lower()
            if contract and side in {"long", "short"}:
                qualified_by_key[(contract, side)] = rank_candidate

        analysed_by_contract: dict[str, dict[str, Any]] = {}
        for contract, item in (scan_analysis or {}).items():
            if isinstance(item, dict):
                analysed_by_contract[str(contract).upper()] = item

        updates: list[dict[str, Any]] = []
        for contract, position in active.items():
            side = self._position_side(position)
            key = f"{contract}:{side}"
            record = await self.repository.get_managed_position(key)
            if record is None:
                # The five-second manager adopts untracked/manual positions.
                # Do not create a second competing plan in the scan path.
                continue
            info = contracts.get(contract)
            if info is None:
                updates.append({
                    "contract": contract,
                    "status": "protection_deferred",
                    "reason": "bitget_contract_metadata_unavailable",
                })
                continue

            selected_candidate: dict[str, Any] | None = qualified_by_key.get((contract, side))
            source = "qualified_ranking"
            if selected_candidate is None:
                analysed = analysed_by_contract.get(contract)
                if analysed and str(analysed.get("direction", "")).lower() == side:
                    selected_candidate = analysed
                    source = "scan_analysis_not_qualified"

            try:
                ticker = await self.gate.rest.get_ticker(contract)
                entry = _number(position.get("entry_price"), _number(record.get("plan", {}).get("entry_price")))
                size = abs(_number(position.get("size")))
                if entry <= 0 or size <= 0:
                    raise TradingRiskError("INVALID_LIVE_POSITION", f"invalid live position for {contract}")

                if selected_candidate is not None:
                    metrics = dict(selected_candidate.get("metrics", {}))
                    metrics["ticker"] = {**metrics.get("ticker", {}), **ticker}
                else:
                    # This is only for symbols that did not reach Gate's
                    # analysis set.  It keeps the exchange protection fresh
                    # without treating absence from the ranking as a signal
                    # reversal.
                    ticker, context = await self._market_context(contract, info)
                    metrics = {
                        "ticker": ticker,
                        "30m": {
                            "atr": context.get("atr30"),
                            "recent_low": context.get("recent_low30"),
                            "recent_high": context.get("recent_high30"),
                            "closed_timestamp": context.get("closed_timestamp30"),
                        },
                        "15m": {
                            "atr": context.get("atr15"),
                            "recent_low": context.get("recent_low15"),
                            "recent_high": context.get("recent_high15"),
                        },
                        "5m": {
                            "atr": context.get("atr5"),
                            "recent_low": context.get("recent_low5"),
                            "recent_high": context.get("recent_high5"),
                        },
                        "volatility_72h": context.get("volatility_72h", {}),
                    }
                    selected_candidate = {
                        "contract": contract,
                        "direction": side,
                        "market_state": record.get("plan", {}).get("market_state", "unknown"),
                        "risk_flags": record.get("plan", {}).get("risk_flags", []),
                        "metrics": metrics,
                    }
                    source = "intraday_fallback"

                actual_notional = notional_from_size(info, entry, size)
                assert selected_candidate is not None
                proposed = build_execution_plan(
                    {**selected_candidate, "metrics": metrics},
                    info,
                    self.settings,
                    entry_price=entry,
                    risk_notional_usdt=actual_notional,
                )
                update = await self._apply_scan_protection_update(
                    record, position, ticker, proposed, info
                )
                plan = record["plan"]
                plan["scan_missing_count"] = 0 if source == "qualified_ranking" else int(
                    _number(plan.get("scan_missing_count"), 0)
                ) + 1
                plan["scan_signal_status"] = {
                    "qualified_ranking": "same_direction_confirmed",
                    "scan_analysis_not_qualified": "same_direction_not_qualified",
                    "intraday_fallback": "not_in_latest_scan_analysis",
                }[source]
                plan["last_scan_seen_at"] = _now().isoformat()
                plan["last_scan_review_at"] = _now().isoformat()
                if source == "qualified_ranking":
                    now = _now()
                    opened_at = self._as_utc_datetime(plan.get("opened_at")) or now
                    horizon = self._time_stop_horizon_hours(plan, self.settings)
                    extension = float(self.settings.time_stop_confirmation_extension_hours)
                    current_deadline = self._as_utc_datetime(plan.get("time_stop_deadline")) or now
                    hard_deadline = opened_at + timedelta(
                        hours=float(self.settings.time_stop_max_holding_hours)
                    )
                    refreshed_deadline = min(
                        hard_deadline,
                        max(current_deadline, now + timedelta(hours=max(horizon, extension))),
                    )
                    plan["last_trend_confirmed_at"] = now.isoformat()
                    plan["time_stop_deadline"] = refreshed_deadline.isoformat()
                    plan["trend_confirmations"] = int(
                        _number(plan.get("trend_confirmations"), 0)
                    ) + 1
                    update["time_stop_refreshed"] = True
                    update["time_stop_deadline"] = plan["time_stop_deadline"]
                    update["trend_confirmations"] = plan["trend_confirmations"]
                record["plan"] = plan
                await self.repository.save_managed_position(record)
                update["scan_source"] = source
                update["scan_recalculated"] = True
                updates.append(update)
            except TradingRiskError as exc:
                updates.append({
                    "contract": contract,
                    "status": "unchanged",
                    "code": exc.code,
                    "error": str(exc),
                    "scan_source": source,
                })
            except Exception as exc:
                logger.exception("scan-time protection update failed for %s", contract)
                updates.append({
                    "contract": contract,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc) or type(exc).__name__,
                    "scan_source": source,
                })
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
        proposed_stop = _number(proposed.get("initial_stop"))
        # Scan refreshes are analysis inputs, not a second trailing engine.
        # Persist the latest 30m invalidation for audit/Discord, while the
        # five-second position manager remains the sole authority allowed to
        # ratchet an exchange stop. This prevents the scanner and trailing
        # policy from replacing each other's orders in the same market move.
        plan["latest_scan_invalidation"] = proposed_stop
        plan["latest_scan_stop_source"] = proposed.get("stop_source")
        plan["latest_scan_stop_components"] = proposed.get("stop_components", {})

        plan["latest_scan_targets"] = proposed.get("take_profits", [])

        plan["market_state"] = proposed.get("market_state", plan.get("market_state"))
        plan["risk_flags"] = list(proposed.get("risk_flags", plan.get("risk_flags", [])))
        plan["ranking_score"] = proposed.get("ranking_score", plan.get("ranking_score"))
        plan["atr15"] = proposed.get("atr15", plan.get("atr15"))
        plan["atr30"] = proposed.get("atr30", plan.get("atr30"))
        plan["atr5"] = proposed.get("atr5", plan.get("atr5"))
        plan["planning_atr"] = proposed.get(
            "planning_atr", plan.get("planning_atr")
        )
        plan["volatility_profile"] = proposed.get(
            "volatility_profile", plan.get("volatility_profile", {})
        )
        record["plan"] = plan
        record["current_size"] = size
        record["updated_at"] = _now().isoformat()
        await self.repository.save_managed_position(record)
        return {
            "contract": contract,
            "status": "unchanged",
            "changed": [],
            "current_r": current_r,
            "protection_status": record.get("protection_status", "exchange"),
            "errors": [],
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

    async def _submit_entry_with_recovery(
        self,
        *,
        contract: str,
        body: dict[str, Any],
        client_id: str,
    ) -> dict[str, Any]:
        """Submit once and recover an uncertain response by clientOid.

        A timeout does not prove that Bitget rejected the order. Looking it up
        by the caller-stable clientOid prevents a second scan from submitting
        a duplicate position after the exchange accepted the first request.
        """
        submit_error: Exception | None = None
        response: dict[str, Any] | None = None
        try:
            response = await self.gate.rest.place_futures_order(body)
        except Exception as exc:
            submit_error = exc

        if response and _order_id(response):
            return response

        detail_reader = getattr(self.gate.rest, "get_order_detail", None)
        if detail_reader is not None:
            try:
                detail = await detail_reader(
                    client_oid=client_id,
                    contract=contract,
                )
                if detail and _order_id(detail):
                    return {
                        "id": _order_id(detail),
                        "id_string": _order_id(detail),
                        "clientOid": detail.get("clientOid") or client_id,
                        "status": detail.get("state") or "recovered",
                        "recovered_after_uncertain_submit": True,
                        "raw": detail,
                    }
            except Exception as recovery_exc:
                logger.warning(
                    "Bitget entry recovery by clientOid failed for %s: %s",
                    contract,
                    type(recovery_exc).__name__,
                )
        try:
            open_orders = await self.gate.rest.get_open_orders(contract)
            recovered = next(
                (
                    item
                    for item in open_orders
                    if str(item.get("text") or item.get("clientOid") or "")
                    == client_id
                    and _order_id(item)
                ),
                None,
            )
            if recovered is not None:
                return {
                    "id": _order_id(recovered),
                    "id_string": _order_id(recovered),
                    "clientOid": client_id,
                    "status": recovered.get("state") or "recovered",
                    "recovered_after_uncertain_submit": True,
                    "raw": recovered,
                }
        except Exception as recovery_exc:
            logger.warning(
                "Bitget pending-order recovery failed for %s: %s",
                contract,
                type(recovery_exc).__name__,
            )
        if submit_error is not None:
            raise submit_error
        raise TradingRiskError(
            "ENTRY_ORDER_ID_MISSING",
            "Bitget did not return or expose an order id for the submitted limit entry",
        )

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
        try:
            order_book = await self.gate.rest.get_order_book(info.name, limit=50)
        except Exception as exc:
            logger.warning("Bitget order-book depth unavailable for %s: %s", info.name, type(exc).__name__)
            order_book = {}
        ticker_metrics = {**candidate.get("metrics", {}), "ticker": {**candidate.get("metrics", {}).get("ticker", {}), **ticker}}
        ticker_metrics["order_book"] = order_book
        preliminary_plan = build_execution_plan(
            {**candidate, "metrics": ticker_metrics},
            info,
            self.settings,
            limit_price,
        )
        tiers = None
        if self.settings.require_max_leverage:
            try:
                tiers = await self.gate.rest.get_risk_limit_tiers(info.name)
            except Exception:
                logger.warning("risk limit tiers unavailable for %s; using contract maximum", info.name)
        leverage = max_leverage_for_notional(info, tiers, 0)
        if leverage is None:
            tiers = await self.gate.rest.get_risk_limit_tiers(info.name)
            leverage = max_leverage_for_notional(info, tiers, 0)
        if leverage is None and self.settings.require_max_leverage:
            raise TradingRiskError("MAX_LEVERAGE_UNAVAILABLE", "Bitget maximum leverage could not be detected")
        if leverage is None:
            leverage = 1.0
        leverage = adaptive_operational_leverage(
            leverage,
            limit_price,
            float(preliminary_plan["initial_stop"]),
            self.settings,
            minimum_leverage=_number(getattr(info, "leverage_min", None), 1.0),
        )
        account = await self.gate.rest.get_account()
        equity = _number(account.get("total"))
        available_margin = _number(account.get("available"))
        if equity <= 0 or available_margin <= 0:
            raise TradingRiskError(
                "ACCOUNT_EQUITY_UNAVAILABLE",
                "Bitget account equity/available margin is required for dynamic position sizing",
            )
        positions = await self.gate.rest.get_positions()
        open_notional = sum(
            abs(_number(item.get("margin"))) * max(1.0, _number(item.get("leverage"), 1.0))
            for item in positions
        )
        mode = getattr(self, "_runtime_mode", str(getattr(self.settings, "trading_mode", "live"))).lower()
        sizing = dynamic_position_notional(
            equity=equity,
            available_margin=available_margin,
            entry=limit_price,
            stop=float(preliminary_plan["initial_stop"]),
            metrics=ticker_metrics,
            side=side,
            stop_quality=str(preliminary_plan["stop_quality"]),
            market_state=str(candidate.get("market_state", "normal")),
            risk_flags=list(candidate.get("risk_flags", [])),
            settings=self.settings,
            open_notional=open_notional,
            leverage=leverage,
            mode=mode,
        )
        notional = float(sizing["notional"])
        # The applicable risk tier can lower leverage and therefore the margin
        # capacity. Recalculate once with that real tier before placing.
        tier_leverage = max_leverage_for_notional(info, tiers, notional) or leverage
        leverage = adaptive_operational_leverage(
            min(leverage, tier_leverage),
            limit_price,
            float(preliminary_plan["initial_stop"]),
            self.settings,
            minimum_leverage=_number(getattr(info, "leverage_min", None), 1.0),
        )
        sizing = dynamic_position_notional(
            equity=equity,
            available_margin=available_margin,
            entry=limit_price,
            stop=float(preliminary_plan["initial_stop"]),
            metrics=ticker_metrics,
            side=side,
            stop_quality=str(preliminary_plan["stop_quality"]),
            market_state=str(candidate.get("market_state", "normal")),
            risk_flags=list(candidate.get("risk_flags", [])),
            settings=self.settings,
            open_notional=open_notional,
            leverage=leverage,
            mode=mode,
        )
        notional = float(sizing["notional"])
        size, actual_notional = notional_for_contract(info, limit_price, notional)
        plan = build_execution_plan(
            {**candidate, "metrics": ticker_metrics},
            info,
            self.settings,
            limit_price,
            risk_notional_usdt=actual_notional,
        )
        plan["position_sizing"] = sizing
        plan["account_equity_at_entry"] = equity
        await self._ensure_single_position_mode()
        await self._ensure_cross_margin(info.name, leverage)
        plan["margin_mode"] = "cross"
        max_open_reader = getattr(
            self.gate.rest,
            "get_max_openable_quantity",
            None,
        )
        if max_open_reader is not None:
            try:
                max_open_size = float(
                    await max_open_reader(info.name, plan["side"], limit_price)
                )
            except Exception as exc:
                max_open_size = None
                logger.warning(
                    "Bitget max-open preflight unavailable for %s: %s",
                    info.name,
                    type(exc).__name__,
                )
            if max_open_size is not None:
                if not isfinite(max_open_size) or max_open_size <= 0:
                    raise TradingRiskError(
                        "EXCHANGE_OPEN_CAPACITY_ZERO",
                        f"Bitget reports no openable quantity for {info.name}",
                    )
                sizing["exchange_max_open_size"] = max_open_size
                if float(size) > max_open_size:
                    capped_notional = notional_from_size(
                        info,
                        limit_price,
                        max_open_size,
                    )
                    size, actual_notional = notional_for_contract(
                        info,
                        limit_price,
                        capped_notional,
                    )
                    viable_notional = (
                        equity
                        * float(
                            self.settings.minimum_viable_position_equity_multiple
                        )
                        * (
                            float(self.settings.test_mode_notional_multiplier)
                            if mode == "test"
                            else 1.0
                        )
                    )
                    if actual_notional + 1e-9 < viable_notional:
                        raise TradingRiskError(
                            "EXCHANGE_CAPACITY_BELOW_VIABLE_POSITION",
                            (
                                f"Bitget capacity allows {actual_notional:.2f} USDT, "
                                f"below the mode-adjusted viable position "
                                f"{viable_notional:.2f} USDT"
                            ),
                        )
                    sizing["exchange_capacity_capped"] = True
                    sizing["notional"] = actual_notional
                    sizing["actual_risk_pct_equity"] = (
                        actual_notional
                        * abs(limit_price - float(preliminary_plan["initial_stop"]))
                        / limit_price
                        / equity
                    )
                    plan = build_execution_plan(
                        {**candidate, "metrics": ticker_metrics},
                        info,
                        self.settings,
                        limit_price,
                        risk_notional_usdt=actual_notional,
                    )
                    plan["position_sizing"] = sizing
                    plan["account_equity_at_entry"] = equity
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
        }
        response = await self._submit_entry_with_recovery(
            contract=info.name,
            body=body,
            client_id=client_id,
        )
        entry_order_id = _order_id(response)
        assert entry_order_id is not None
        atr_pct = (
            _number(plan.get("planning_atr"), _number(plan.get("atr15")))
            / limit_price
            if limit_price > 0
            else 0.0
        )
        volatility_move_threshold = min(
            0.05,
            max(
                float(self.settings.limit_entry_min_cancel_move_pct),
                1.5 * atr_pct,
            ),
        )
        self._pending_entry_metadata[entry_order_id] = {
            "volatility_move_threshold": volatility_move_threshold,
            "client_id": client_id,
        }
        self._pending_entry_metadata[client_id] = {
            "volatility_move_threshold": volatility_move_threshold,
            "order_id": entry_order_id,
        }
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
            self._pending_entry_metadata.pop(entry_order_id, None)
            self._pending_entry_metadata.pop(client_id, None)
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
                "position_sizing": sizing,
                "entry_order_id": entry_order_id,
                "pending_entry": True,
                "protection_status": "pending_until_fill",
                "entry_confirmation": confirmed_order or order_detail,
                "stop_loss": plan.get("initial_stop"),
                "stop_source": plan.get("stop_source"),
                "planning_atr": plan.get("planning_atr"),
                "volatility_regime": (
                    plan.get("volatility_profile", {}).get("regime", "normal")
                    if isinstance(plan.get("volatility_profile"), dict)
                    else "normal"
                ),
                "take_profit_allocation_source": plan.get(
                    "take_profit_allocation_source"
                ),
            }
            return action
        self._pending_entry_metadata.pop(entry_order_id, None)
        self._pending_entry_metadata.pop(client_id, None)
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
            "position_sizing": sizing,
            "margin_mode": plan.get("margin_mode", "cross"),
            "stop_loss": plan["initial_stop"],
            "stop_source": plan.get("stop_source"),
            "planning_atr": plan.get("planning_atr"),
            "volatility_regime": (
                plan.get("volatility_profile", {}).get("regime", "normal")
                if isinstance(plan.get("volatility_profile"), dict)
                else "normal"
            ),
            "take_profits": plan["take_profits"],
            "take_profit_allocation_source": plan.get(
                "take_profit_allocation_source"
            ),
            "time_stop_horizon_hours": self._time_stop_horizon_hours(
                plan, self.settings
            ),
            "position_key": position_key,
            "protection_status": protection_status,
        }
        if protection_error:
            action["protection_error"] = protection_error
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
        plan["entry_size"] = entry_size
        plan["initial_position_size"] = entry_size
        created_ids: list[str] = []
        try:
            stop_id = await self._create_trigger(
                plan, "stop", plan["current_stop"], str(entry_size), position_key
            )
            created_ids.append(stop_id)
            plan["protection_order_ids"]["stop"] = stop_id
            tp_sizes = planned_take_profit_sizes(plan, entry_size)
            for target in plan["take_profits"]:
                stage = target["stage"]
                size = tp_sizes.get(stage)
                if size is None:
                    continue
                order_id = await self._create_trigger(
                    plan, stage, target["price"], size, position_key
                )
                created_ids.append(order_id)
                plan["protection_order_ids"][stage] = order_id
            await self._verify_exchange_protection(plan)
            try:
                await self._remove_redundant_entry_presets(
                    plan, entry_size, set(created_ids)
                )
            except Exception as cleanup_exc:
                # The verified managed stop must remain live even if Bitget
                # temporarily refuses cleanup of the older fill-gap preset.
                logger.warning(
                    "redundant entry preset cleanup delayed for %s: %s",
                    contract,
                    type(cleanup_exc).__name__,
                )
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

    async def _remove_redundant_entry_presets(
        self, plan: dict[str, Any], entry_size: float, protected_ids: set[str]
    ) -> None:
        """Remove the fill-gap preset after the managed protection set is live."""
        orders = await self.gate.rest.get_price_orders(
            status="open", contract=plan["contract"]
        )
        tick = max(
            _number(plan.get("price_tick")),
            abs(_number(plan.get("entry_price"))) * 1e-7,
        )
        expected = {
            "loss_plan": _number(plan.get("initial_stop")),
            # Older deployments also attached a full-size preset TP1.
            "profit_plan": _number(plan.get("take_profits", [{}])[0].get("price")),
        }
        for order in orders:
            order_id = str(_order_id(order) or "")
            if not order_id or order_id in protected_ids:
                continue
            raw = order.get("raw", {}) if isinstance(order.get("raw"), dict) else {}
            plan_type = str(order.get("plan_type") or raw.get("planType") or "").lower()
            trigger = order.get("trigger") if isinstance(order.get("trigger"), dict) else {}
            trigger_price = _number(trigger.get("price"))
            initial = order.get("initial") if isinstance(order.get("initial"), dict) else {}
            order_size = abs(_number(initial.get("size")))
            client_oid = str(order.get("text") or order.get("clientOid") or "").lower()
            matches_entry = client_oid.startswith("t-auto-entry-")
            target_price = expected.get(plan_type, 0.0)
            matches_legacy_preset = (
                target_price > 0
                and abs(trigger_price - target_price)
                <= max(tick, target_price * 0.0001)
                and abs(order_size - entry_size)
                <= max(entry_size * 0.01, 1e-12)
            )
            if matches_entry or matches_legacy_preset:
                await self.gate.rest.cancel_price_order(order_id)

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
            if plan.get("enable_decimal"):
                initial["amount"] = size
            else:
                integer_size = int(Decimal(str(size)))
                if integer_size == 0:
                    raise TradingRiskError(
                        "PROTECTION_SIZE_ZERO",
                        f"calculated protection size is zero for stop on {plan['contract']}",
                    )
                initial["size"] = integer_size
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
            # Stops use mark price so a last-trade wick cannot bypass the
            # exchange-side loss guard; profit targets use fill price so a
            # brief mark-price distortion does not take profit prematurely.
            "price_type": 1 if is_stop else 0,
            "price": _decimal_text(trigger_price),
            "rule": rule,
        }
        expiration = int(self.settings.order_trigger_expiration_seconds)
        if expiration > 0:
            trigger["expiration"] = expiration
        protection_body = {
            "initial": initial,
            "trigger": trigger,
            "order_type": order_type,
            "pos_margin_mode": plan.get("margin_mode", "cross"),
        }
        create_error: Exception | None = None
        try:
            response = await self.gate.rest.create_price_order(protection_body)
        except Exception as exc:
            response = {}
            create_error = exc
        order_id = _order_id(response)
        if not order_id:
            try:
                try:
                    open_orders = await self.gate.rest.get_price_orders(
                        status="open",
                        contract=plan["contract"],
                        client_oid=text,
                    )
                except TypeError:
                    open_orders = await self.gate.rest.get_price_orders(
                        status="open",
                        contract=plan["contract"],
                    )
                recovered = next(
                    (
                        item
                        for item in open_orders
                        if str(item.get("text") or item.get("clientOid") or "")
                        == text
                        and _order_id(item)
                    ),
                    None,
                )
                if recovered is not None:
                    order_id = _order_id(recovered)
            except Exception as recovery_exc:
                logger.warning(
                    "Bitget %s recovery by clientOid failed for %s: %s",
                    kind,
                    plan["contract"],
                    type(recovery_exc).__name__,
                )
        if not order_id:
            if create_error is not None:
                raise create_error
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

    async def _wait_for_no_price_orders(self, contract: str, attempts: int = 8) -> bool:
        """Wait until Bitget confirms that no trigger orders remain."""
        for attempt in range(max(1, attempts)):
            open_orders = await self.gate.rest.get_price_orders(status="open", contract=contract)
            if not open_orders:
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
        expected: dict[str, tuple[str, float, str]] = {}
        tp_sizes = planned_take_profit_sizes(plan, plan.get("entry_size") or 0)
        if ids.get("stop") not in (None, ""):
            expected["stop"] = (str(ids["stop"]), _number(plan.get("current_stop")), str(plan.get("entry_size") or 0))
        for target in plan.get("take_profits", []):
            stage = str(target["stage"])
            if stage in tp_sizes and ids.get(stage) not in (None, ""):
                expected[stage] = (
                    str(ids[stage]),
                    _number(target.get("price")),
                    tp_sizes[stage],
                )
        if not expected:
            raise TradingRiskError("PROTECTION_ORDER_ID_MISSING", f"no exchange protection ids for {plan['contract']}")
        for attempt in range(5):
            open_orders = await self.gate.rest.get_price_orders(status="open", contract=plan["contract"])
            visible = {_order_id(item): item for item in open_orders if _order_id(item)}
            invalid = [
                stage
                for stage, (order_id, price, size) in expected.items()
                if order_id not in visible or not self._protection_order_matches(plan, stage, price, size, visible[order_id])
            ]
            if not invalid:
                return
            if attempt < 4:
                await asyncio.sleep(0.2 * (attempt + 1))
        missing = sorted(invalid)
        missing_ids = [stage for stage, (order_id, _, _) in expected.items() if order_id not in visible]
        raise TradingRiskError(
            "PROTECTION_ORDER_NOT_CONFIRMED" if missing_ids else "PROTECTION_ORDER_MISMATCH",
            f"Bitget did not confirm valid exchange protection orders for {plan['contract']}: {','.join(missing)}",
        )

    @staticmethod
    def _protection_order_matches(
        plan: dict[str, Any], stage: str, expected_price: float, expected_size: str, order: dict[str, Any]
    ) -> bool:
        """Validate the actual Bitget trigger, not only its returned ID."""
        raw_value = order.get("raw")
        raw: dict[str, Any] = dict(raw_value) if isinstance(raw_value, dict) else {}
        expected_plan_type = "loss_plan" if stage == "stop" else "profit_plan"
        actual_plan_type = str(order.get("plan_type") or raw.get("planType") or "").lower()
        if actual_plan_type and actual_plan_type != expected_plan_type:
            return False

        trigger_value = order.get("trigger")
        trigger: dict[str, Any] = dict(trigger_value) if isinstance(trigger_value, dict) else {}
        actual_price = _number(trigger.get("price"))
        if actual_price <= 0:
            actual_price = _number(
                raw.get("triggerPrice")
                or (raw.get("stopLossTriggerPrice") if stage == "stop" else raw.get("stopSurplusTriggerPrice"))
            )
        if actual_price > 0 and expected_price > 0:
            tick = _number(plan.get("price_tick"))
            tolerance = max(tick * 1.5, abs(expected_price) * 1e-8, 1e-12)
            if abs(actual_price - expected_price) > tolerance:
                return False

        expected_price_type = 1 if stage == "stop" else 0
        actual_price_type = trigger.get("price_type")
        if actual_price_type is None:
            trigger_type = raw.get("triggerType")
            if trigger_type not in (None, ""):
                actual_price_type = 1 if str(trigger_type).lower() == "mark_price" else 0
        if actual_price_type is not None and int(actual_price_type) != expected_price_type:
            return False

        margin_mode = str(order.get("margin_mode") or raw.get("marginMode") or "").lower()
        if margin_mode and margin_mode not in {"cross", "crossed"}:
            return False
        position_mode = str(order.get("mode") or raw.get("posMode") or "").lower()
        if position_mode and position_mode not in {"single", "one_way_mode"}:
            return False

        actual_size = _number((order.get("initial") or {}).get("size"))
        expected_size_number = abs(_number(expected_size))
        if actual_size > 0 and expected_size_number > 0:
            if abs(actual_size - expected_size_number) > max(expected_size_number * 0.005, 1e-12):
                return False
        return True

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
            self._pending_momentum_observations.clear()
            self._pending_entry_metadata.clear()
            return []
        pending_ids = {_order_id(item) for item in pending}
        self._pending_momentum_observations = {
            key: value
            for key, value in self._pending_momentum_observations.items()
            if key in pending_ids
        }
        pending_client_ids = {str(item.get("text") or item.get("clientOid") or "") for item in pending}
        self._pending_entry_metadata = {
            key: value
            for key, value in self._pending_entry_metadata.items()
            if key in pending_ids or key in pending_client_ids
        }
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
            observation_count = 0
            metadata = self._pending_entry_metadata.get(order_id) or self._pending_entry_metadata.get(
                str(order.get("text") or order.get("clientOid") or "")
            ) or {}
            cancel_move_threshold = max(
                float(self.settings.limit_entry_min_cancel_move_pct),
                float(self.settings.limit_entry_cancel_move_pct),
                _number(metadata.get("volatility_move_threshold")),
            )
            hard_move_threshold = max(
                cancel_move_threshold + 0.01,
                float(self.settings.limit_entry_hard_move_pct),
            )
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
                previous = self._pending_momentum_observations.get(order_id)
                now_monotonic = time.monotonic()
                if directional_move < cancel_move_threshold:
                    # The market returned to the normal entry area.  Reset
                    # the streak so one old excursion cannot trigger a later
                    # cancellation.
                    self._pending_momentum_observations.pop(order_id, None)
                else:
                    if previous and now_monotonic - float(previous.get("observed_at", 0)) <= 30:
                        observation_count = int(previous.get("count", 0)) + 1
                    else:
                        observation_count = 1
                    self._pending_momentum_observations[order_id] = {
                        "count": observation_count,
                        "observed_at": now_monotonic,
                        "directional_move": directional_move,
                    }
                    # Ordinary crypto volatility receives a grace period and
                    # consecutive confirmation.  Only a genuinely large
                    # one-way move may cancel sooner, and it still has a
                    # non-zero observation window.
                    hard_move = directional_move >= hard_move_threshold
                    hard_ready = age >= int(self.settings.limit_entry_hard_move_min_observation_seconds)
                    normal_ready = age >= int(self.settings.limit_entry_min_observation_seconds)
                    confirmations = max(1, int(self.settings.limit_entry_momentum_confirmations))
                    if (hard_move and hard_ready) or (normal_ready and observation_count >= confirmations):
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
                        "observation_count": observation_count,
                        "move_threshold": cancel_move_threshold,
                    })
                    continue
                # A fill can race the cancel request.  Re-read the position so
                # a successful cancel response is never reported as a failed
                # entry when Bitget actually filled it in the same moment.
                filled_position = await self.gate.rest.get_position(contract)
                if filled_position and abs(_number(filled_position.get("size"))) > 0:
                    actions.append({
                        "contract": contract,
                        "status": "limit_order_filled_during_cancel",
                        "code": "LIMIT_ENTRY_FILLED",
                        "side": side,
                        "entry_order_id": order_id,
                        "entry_limit_price": limit_price,
                        "age_seconds": round(age, 1),
                        "notify": False,
                    })
                    self._pending_momentum_observations.pop(order_id, None)
                    self._pending_entry_metadata.pop(order_id, None)
                    self._pending_entry_metadata.pop(str(order.get("text") or order.get("clientOid") or ""), None)
                    continue
                action = {
                    "contract": contract,
                    "status": "limit_order_cancelled",
                    "code": reason,
                    "side": side,
                    "entry_order_id": order_id,
                    "entry_limit_price": limit_price,
                    "age_seconds": round(age, 1),
                    "observation_count": observation_count,
                    "move_threshold": cancel_move_threshold,
                    "hard_move_threshold": hard_move_threshold,
                    "cancel_response": cancel_response,
                    "cancel_confirmed": True,
                }
                actions.append(action)
                self._pending_momentum_observations.pop(order_id, None)
                self._pending_entry_metadata.pop(order_id, None)
                self._pending_entry_metadata.pop(str(order.get("text") or order.get("clientOid") or ""), None)
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
        try:
            contracts = await self._contracts()
        except Exception as exc:
            # Protection repair must not wait for the public contract list.
            # Existing managed plans already contain all data needed to
            # restore their exchange-side triggers.
            logger.warning("Bitget contract metadata unavailable during management: %s", type(exc).__name__)
            contracts = {}
        managed = await self.repository.list_managed_positions(active_only=True)
        managed_keys = {item.get("position_key") for item in managed}
        actions: list[dict[str, Any]] = []
        for contract, position in active.items():
            size = _number(position.get("size"))
            side = "long" if size > 0 else "short"
            key = f"{contract}:{side}"
            plan_record = await self.repository.get_managed_position(key)
            info = contracts.get(contract)
            if info is None:
                if plan_record is not None:
                    actions.append(await self._repair_protection_only(plan_record, position))
                continue
            try:
                ticker, context = await self._market_context(contract, info)
            except Exception as exc:
                logger.warning("market data invalid for managed position %s: %s", contract, type(exc).__name__)
                if plan_record is not None:
                    repair = await self._repair_protection_only(plan_record, position)
                    repair["market_data_error"] = type(exc).__name__
                    actions.append(repair)
                else:
                    actions.append({"contract": contract, "status": "data_invalid", "error": type(exc).__name__})
                continue
            if plan_record is None:
                plan = self._plan_from_position(position, ticker, context, info)
                protection_status = "exchange"
                protection_error = None
                try:
                    # Adopt any existing exchange protections first.  This
                    # covers manual positions as well as positions opened by
                    # this service; missing or incorrect stages are then
                    # created/replaced by the normal reconciliation path.
                    await self._seed_existing_protection_ids(plan, contract)
                    await self._ensure_protection(plan, abs(size), key)
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

    async def _seed_existing_protection_ids(self, plan: dict[str, Any], contract: str) -> None:
        """Bind existing Bitget SL/TP orders before reconciling an adopted position.

        The exchange scopes pending TPSL orders by contract/position.  When a
        position was opened manually, its existing loss/profit plans can be
        safely adopted instead of blindly deleting them.  Orders that do not
        match the current strategy are replaced only after the new order has
        been confirmed.
        """
        open_orders = await self.gate.rest.get_price_orders(status="open", contract=contract)
        ids = plan.setdefault("protection_order_ids", {})
        for order in open_orders:
            stage = self._protection_stage_from_order(order)
            order_id = _order_id(order)
            if stage in ids and ids.get(stage) in (None, "") and order_id:
                ids[stage] = order_id

        entry = _number(plan.get("entry_price"))
        side = str(plan.get("side", "")).lower()

        def trigger_price(order: dict[str, Any]) -> float:
            trigger_value = order.get("trigger")
            trigger: dict[str, Any] = dict(trigger_value) if isinstance(trigger_value, dict) else {}
            return _number(trigger.get("price"))

        loss_orders = [
            item for item in open_orders
            if str(item.get("plan_type") or item.get("raw", {}).get("planType") or "").lower() == "loss_plan"
            and _order_id(item)
        ]
        if not ids.get("stop"):
            loss_orders = [
                item for item in loss_orders
                if (
                    trigger_price(item) < entry if side == "long" else trigger_price(item) > entry
                )
            ]
            if loss_orders:
                ids["stop"] = _order_id(loss_orders[0])

        profit_orders = [
            item for item in open_orders
            if str(item.get("plan_type") or item.get("raw", {}).get("planType") or "").lower() == "profit_plan"
            and _order_id(item)
            and _order_id(item) not in set(ids.values())
            and (
                trigger_price(item) > entry if side == "long" else trigger_price(item) < entry
            )
        ]
        profit_orders.sort(key=trigger_price, reverse=side == "short")
        for target in plan.get("take_profits", []):
            stage = str(target.get("stage", ""))
            if stage and not ids.get(stage) and profit_orders:
                ids[stage] = _order_id(profit_orders.pop(0))

    async def _repair_protection_only(self, record: dict[str, Any], position: dict[str, Any]) -> dict[str, Any]:
        """Restore exchange triggers even when fresh market data is unavailable."""
        plan = record.get("plan") or {}
        contract = str(record.get("contract") or plan.get("contract") or position.get("contract") or "")
        size = abs(_number(position.get("size")))
        try:
            await self._ensure_protection(plan, size, str(record.get("position_key") or f"{contract}:{plan.get('side')}"))
            record["plan"] = plan
            record["current_size"] = size
            record["protection_status"] = "exchange"
            record.pop("protection_error", None)
            record["updated_at"] = _now().isoformat()
            await self.repository.save_managed_position(record)
            return {
                "contract": contract,
                "status": "protection_reconciled_without_market_data",
                "protection_status": "exchange",
            }
        except Exception as exc:
            record["protection_status"] = "backend_fallback"
            record["protection_error"] = f"{type(exc).__name__}: {exc}"
            record["updated_at"] = _now().isoformat()
            await self.repository.save_managed_position(record)
            logger.exception("exchange protection-only repair failed for %s", contract)
            return {
                "contract": contract,
                "status": "protection_repair_failed",
                "protection_status": "backend_fallback",
                "error_type": type(exc).__name__,
                "error": str(exc) or type(exc).__name__,
            }

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
        if not await self._wait_for_no_price_orders(contract):
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
            try:
                protected_ids = {
                    str(value)
                    for value in plan.get("protection_order_ids", {}).values()
                    if value not in (None, "")
                }
                await self._remove_redundant_entry_presets(
                    plan,
                    _number(plan.get("initial_position_size"), live_size),
                    protected_ids,
                )
            except Exception as cleanup_exc:
                logger.warning(
                    "redundant preset reconciliation delayed for %s: %s",
                    plan["contract"],
                    type(cleanup_exc).__name__,
                )
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
        trend_assessment = self._trend_break_assessment(plan, context)
        trend_confirmation = self._update_thesis_failure_confirmation(
            plan,
            trend_assessment,
            int(getattr(self.settings, "thesis_soft_failure_confirmations", 2)),
        )
        trend_break_score = int(trend_assessment["score"])
        plan["trend_break_score"] = trend_break_score
        plan["trend_break_assessment"] = trend_assessment
        plan["thesis_failure_confirmations"] = trend_confirmation["confirmations"]
        plan["thesis_failure_confirmed"] = trend_confirmation["confirmed"]
        plan["trend_checked_at"] = _now().isoformat()
        now = _now()
        opened_at = self._as_utc_datetime(plan.get("opened_at")) or now
        deadline = self._as_utc_datetime(plan.get("time_stop_deadline"))
        if deadline is None:
            deadline = now + timedelta(
                hours=self._time_stop_horizon_hours(plan, self.settings)
            )
            plan["time_stop_deadline"] = deadline.isoformat()
        holding_hours = max(0.0, (now - opened_at).total_seconds() / 3600)
        time_remaining_hours = (deadline - now).total_seconds() / 3600
        plan["holding_hours"] = holding_hours
        plan["time_remaining_hours"] = time_remaining_hours

        # Time is capital. If the expected move has not developed before the
        # signal's regime-aware deadline, release the position rather than
        # letting a stale thesis consume margin. A fresh qualified same-side
        # scan refreshes this deadline in _synchronize_positions_from_scan.
        if time_remaining_hours <= 0 and current_r < float(self.settings.time_stop_min_progress_r):
            try:
                await self._close_for_trend_break(plan["contract"], position)
                record["status"] = "closed"
                record["closed_at"] = now.isoformat()
                plan["phase"] = "TIME_DECAY_EXIT"
                record["plan"] = plan
                record["updated_at"] = now.isoformat()
                await self.repository.save_managed_position(record)
                action = {
                    "contract": plan["contract"],
                    "status": "time_decay_closed",
                    "reason": "regime-adjusted holding window expired before minimum trend progress",
                    "side": plan["side"],
                    "current_r": current_r,
                    "holding_hours": round(holding_hours, 2),
                    "time_remaining_hours": round(time_remaining_hours, 2),
                    "trend_confirmations": int(_number(plan.get("trend_confirmations"), 0)),
                    "trend_break_score": trend_break_score,
                }
                await self.repository.save_order_event(
                    {
                        "event_id": uuid.uuid4().hex,
                        "client_order_id": None,
                        "contract": plan["contract"],
                        "event_type": action["status"],
                        "created_at": now,
                        "payload": action,
                    }
                )
                await self._notify_order(action)
                return action
            except Exception as exc:
                protection_error = protection_error or f"time_decay_close:{type(exc).__name__}: {exc}"
                logger.exception("time-decay close failed for %s", plan["contract"])
        elif time_remaining_hours <= 0:
            hard_deadline = opened_at + timedelta(
                hours=float(self.settings.time_stop_max_holding_hours)
            )
            grace_deadline = min(
                hard_deadline,
                now + timedelta(hours=float(self.settings.time_stop_confirmation_extension_hours)),
            )
            if grace_deadline > now:
                plan["time_stop_deadline"] = grace_deadline.isoformat()
                plan["time_decay_grace_count"] = int(
                    _number(plan.get("time_decay_grace_count"), 0)
                ) + 1
                deadline = grace_deadline
                time_remaining_hours = (deadline - now).total_seconds() / 3600
                await self._notify_decisions(
                    [
                        {
                            "contract": plan["contract"],
                            "status": "time_decay_grace",
                            "reason": "minimum trend progress achieved; limited grace window granted",
                            "side": plan["side"],
                            "current_r": current_r,
                            "holding_hours": round(holding_hours, 2),
                            "time_remaining_hours": round(time_remaining_hours, 2),
                            "trend_confirmations": int(
                                _number(plan.get("trend_confirmations"), 0)
                            ),
                        }
                    ]
                )

        # The scanner enters from a 4h environment and a 30m thesis. A 5m
        # fluctuation is therefore only a warning; it cannot overturn the
        # position. A closed 30m structure failure with 15m confirmation can
        # exit immediately, while a softer EMA rollover needs consecutive
        # completed 30m observations. This is evidence-based, not a time lock.
        if bool(trend_confirmation["confirmed"]) and current_r < 0:
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
                    "reason": "closed 30m scan thesis failed with 15m confirmation",
                    "trend_break_score": trend_break_score,
                    "thesis_failure_kind": trend_assessment["failure_kind"],
                    "thesis_failure_confirmations": trend_confirmation["confirmations"],
                    "thesis_failure_evidence": trend_assessment["evidence"],
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

        trail = managed_stop_candidate(
            plan=plan,
            context=context,
            price=price,
            entry=entry,
            settings=self.settings,
        )
        candidate_stop = _number(trail.get("candidate_stop"), _number(plan.get("current_stop")))
        plan["phase"] = str(trail.get("phase") or plan.get("phase") or "INITIAL_RISK")
        plan["favorable_extreme"] = _number(trail.get("favorable_extreme"), price)
        plan["peak_r_multiple"] = _number(trail.get("peak_r_multiple"))
        plan["trail_source"] = str(trail.get("trail_source") or "initial_invalidation")
        plan["management_atr"] = _number(
            trail.get("management_atr"),
            _number(context.get("atr15"), _number(plan.get("planning_atr"))),
        )
        plan["volatility_regime"] = str(
            trail.get("volatility_regime") or plan.get("volatility_regime") or "normal"
        )
        stop_candidate_better = self._stop_is_better(plan["side"], candidate_stop, _number(plan.get("current_stop")))
        stop_moved = False
        stop_update_locked = False
        last_stop_update = self._as_utc_datetime(plan.get("last_stop_update"))
        if last_stop_update is not None:
            stop_update_locked = (
                _now() - last_stop_update
            ).total_seconds() < float(
                getattr(self.settings, "stop_update_cooldown_seconds", 60)
            )
        stop_move_threshold = float(
            getattr(self.settings, "stop_update_min_atr", 0.20)
        ) * _number(plan.get("management_atr"))
        stop_is_behind_market = (
            candidate_stop < price
            if plan["side"] == "long"
            else candidate_stop > price
        )
        if (
            stop_candidate_better
            and stop_is_behind_market
            and not stop_update_locked
            and abs(candidate_stop - _number(plan.get("current_stop"))) >= stop_move_threshold
        ):
            old_id = plan.get("protection_order_ids", {}).get("stop")
            try:
                new_id = await self._replace_trigger(
                    plan, "stop", candidate_stop, str(live_size), record["position_key"], old_id
                )
                plan["protection_order_ids"]["stop"] = new_id
                plan["current_stop"] = candidate_stop
                plan["last_stop_update"] = _now().isoformat()
                stop_moved = True
            except Exception as exc:
                protection_error = protection_error or f"{type(exc).__name__}: {exc}"
                logger.exception("exchange trailing stop update failed for %s", plan["contract"])

        # TP1/TP2/TP3 are fixed realization orders. They are never chased
        # farther away after entry; continuation is exclusively managed by
        # the runner's volatility trail.
        tp_moved = False
        try:
            weakness_tp_moved = await self._tighten_take_profit_for_weakness(
                record,
                plan,
                price,
                context,
                bool(trend_assessment["profitable_weakness"]),
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
                        "trail_source": plan.get("trail_source"),
                        "favorable_extreme": plan.get("favorable_extreme"),
                        "peak_r_multiple": plan.get("peak_r_multiple"),
                        "fallback_actions": fallback_actions,
                        "trend_break_score": trend_break_score,
                        "thesis_failure_confirmations": trend_confirmation["confirmations"],
                        "volatility_regime": plan.get("volatility_regime"),
                        "management_atr": plan.get("management_atr"),
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
            "thesis_failure_confirmations": trend_confirmation["confirmations"],
            "thesis_failure_confirmed": trend_confirmation["confirmed"],
            "holding_hours": round(holding_hours, 2),
            "time_remaining_hours": round(time_remaining_hours, 2),
            "trend_confirmations": int(_number(plan.get("trend_confirmations"), 0)),
            "current_stop": plan.get("current_stop"),
            "trail_source": plan.get("trail_source"),
            "favorable_extreme": plan.get("favorable_extreme"),
            "peak_r_multiple": plan.get("peak_r_multiple"),
            "volatility_regime": plan.get("volatility_regime"),
            "management_atr": plan.get("management_atr"),
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

        desired: dict[str, tuple[float, str]] = {
            "stop": (_number(plan.get("current_stop")), str(entry_size))
        }
        tp_sizes = planned_take_profit_sizes(plan, entry_size)
        for target in plan.get("take_profits", []):
            stage = str(target.get("stage", ""))
            if stage in tp_sizes:
                desired[stage] = (
                    _number(target.get("price")),
                    tp_sizes[stage],
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
            current_order_valid = (
                current_id in open_ids
                and current_order is not None
                and self._protection_order_matches(plan, stage, price, size, current_order)
            )
            if current_order_valid and not stop_size_changed:
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

        fallback_tp_sizes = planned_take_profit_sizes(plan, fallback_size)
        for target in plan.get("take_profits", []):
            stage = target["stage"]
            if stage not in reached_stages or stage not in fallback_stages:
                continue
            size = fallback_tp_sizes.get(stage)
            if size is None:
                continue
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
                        if action.get("notify", True):
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
        tp_size = planned_take_profit_sizes(
            plan, record.get("current_size", 0)
        ).get(target["stage"])
        if tp_size is None:
            return False
        new_id = await self._replace_trigger(
            plan,
            target["stage"],
            new_price,
            tp_size,
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
        raw30, raw15, raw5 = await asyncio.gather(
            self.gate.rest.get_candlesticks(contract, "30m", limit=180),
            self.gate.rest.get_candlesticks(contract, "15m", limit=100),
            self.gate.rest.get_candlesticks(contract, "5m", limit=100),
        )
        candles30 = closed_candles(normalize_candles(raw30, info.quanto_multiplier), "30m")
        candles15 = closed_candles(normalize_candles(raw15, info.quanto_multiplier), "15m")
        candles5 = closed_candles(normalize_candles(raw5, info.quanto_multiplier), "5m")
        context: dict[str, Any] = {}
        for label, candles in (("30", candles30), ("15", candles15), ("5", candles5)):
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
            context[f"closed_timestamp{label}"] = str(frame["timestamp"].iloc[-1])
            if label == "30":
                context["ema20030"] = float(
                    close.ewm(span=200, adjust=False).mean().iloc[-1]
                )
                context["volatility_72h"] = adaptive_volatility_profile(
                    high,
                    low,
                    close,
                    baseline_bars=int(
                        getattr(self.settings, "volatility_baseline_bars_30m", 144)
                    ),
                    recent_bars=int(
                        getattr(self.settings, "volatility_recent_bars_30m", 12)
                    ),
                    shock_bars=int(
                        getattr(self.settings, "volatility_shock_bars_30m", 4)
                    ),
                    expansion_ratio=float(
                        getattr(self.settings, "volatility_expansion_ratio", 1.50)
                    ),
                    expansion_min_bars=int(
                        getattr(self.settings, "volatility_expansion_min_bars", 3)
                    ),
                )
        self._market_cache[contract] = (time.monotonic(), context)
        return ticker, context

    @staticmethod
    def _trend_break_assessment(
        plan: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        """Assess failure on the same 30m horizon that authorized entry."""
        side = str(plan.get("side", "")).lower()
        last30 = _number(context.get("last_close30"))
        ema20_30 = _number(context.get("ema2030"))
        ema50_30 = _number(context.get("ema5030"))
        last15 = _number(context.get("last_close15"))
        ema20_15 = _number(context.get("ema2015"))
        ema50_15 = _number(context.get("ema5015"))
        last5 = _number(context.get("last_close5"))
        ema20_5 = _number(context.get("ema205"))
        recent_low30 = _number(context.get("recent_low30"))
        recent_high30 = _number(context.get("recent_high30"))
        recent_low15 = _number(context.get("recent_low15"))
        recent_high15 = _number(context.get("recent_high15"))
        if side == "long":
            structure30 = last30 > 0 and recent_low30 > 0 and last30 < recent_low30
            ema30 = (
                last30 > 0
                and ema20_30 > 0
                and ema50_30 > 0
                and last30 < ema20_30 < ema50_30
            )
            structure15 = last15 > 0 and recent_low15 > 0 and last15 < recent_low15
            ema15 = (
                last15 > 0
                and ema20_15 > 0
                and ema50_15 > 0
                and last15 < ema20_15 < ema50_15
            )
            warning5 = (
                last5 > 0
                and ema20_5 > 0
                and last5 < ema20_5
            )
        elif side == "short":
            structure30 = last30 > 0 and recent_high30 > 0 and last30 > recent_high30
            ema30 = (
                last30 > 0
                and ema20_30 > 0
                and ema50_30 > 0
                and last30 > ema20_30 > ema50_30
            )
            structure15 = last15 > 0 and recent_high15 > 0 and last15 > recent_high15
            ema15 = (
                last15 > 0
                and ema20_15 > 0
                and ema50_15 > 0
                and last15 > ema20_15 > ema50_15
            )
            warning5 = (
                last5 > 0
                and ema20_5 > 0
                and last5 > ema20_5
            )
        else:
            structure30 = ema30 = structure15 = ema15 = warning5 = False
        hard_failure = bool(structure30 and (structure15 or ema15))
        soft_failure = bool(ema30 and structure15 and ema15)
        failure_kind = "hard_structure" if hard_failure else "soft_rollover" if soft_failure else "none"
        evidence = [
            name
            for name, present in (
                ("30m_structure", structure30),
                ("30m_ema", ema30),
                ("15m_structure", structure15),
                ("15m_ema", ema15),
                ("5m_warning_only", warning5),
            )
            if present
        ]
        return {
            "score": (
                2 * int(bool(structure30))
                + 2 * int(bool(ema30))
                + int(bool(structure15))
                + int(bool(ema15))
                + int(bool(warning5))
            ),
            "candidate": hard_failure or soft_failure,
            "hard_failure": hard_failure,
            "soft_failure": soft_failure,
            "failure_kind": failure_kind,
            "profitable_weakness": bool(
                (structure30 or ema30) and (structure15 or ema15)
            ),
            "evidence": evidence,
            "observation_key": str(
                context.get("closed_timestamp30")
                or f"{last30:.12g}"
            ),
        }

    @staticmethod
    def _update_thesis_failure_confirmation(
        plan: dict[str, Any],
        assessment: dict[str, Any],
        soft_required: int,
    ) -> dict[str, int | bool]:
        """Count completed 30m observations, never five-second manager ticks."""
        observation = str(assessment.get("observation_key") or "")
        previous_observation = str(plan.get("thesis_failure_observation") or "")
        confirmations = int(_number(plan.get("thesis_failure_confirmations"), 0))
        if observation != previous_observation:
            if assessment.get("candidate"):
                confirmations += 1
            else:
                confirmations = 0
            plan["thesis_failure_observation"] = observation
        required = 1 if assessment.get("hard_failure") else max(2, int(soft_required))
        if assessment.get("hard_failure"):
            confirmations = max(1, confirmations)
        confirmed = bool(assessment.get("candidate")) and confirmations >= required
        plan["thesis_failure_confirmations"] = confirmations
        plan["thesis_failure_confirmed"] = confirmed
        return {
            "confirmations": confirmations,
            "required": required,
            "confirmed": confirmed,
        }

    @staticmethod
    def _trend_break_score(plan: dict[str, Any], context: dict[str, Any]) -> int:
        """Compatibility wrapper for monitoring and older integrations."""
        return int(TradingService._trend_break_assessment(plan, context)["score"])

    async def _tighten_take_profit_for_weakness(
        self,
        record: dict[str, Any],
        plan: dict[str, Any],
        price: float,
        context: dict[str, Any],
        thesis_weakness: bool,
    ) -> bool:
        """Move the next TP closer only after profitable 30m/15m weakness."""
        if not thesis_weakness or _number(plan.get("current_r_multiple")) < 1.0:
            return False
        last_update = plan.get("last_take_profit_update")
        if last_update:
            try:
                if (_now() - datetime.fromisoformat(last_update)).total_seconds() < 300:
                    return False
            except ValueError:
                pass
        management_atr = _number(
            plan.get("management_atr"),
            _number(context.get("atr15"), _number(plan.get("planning_atr"))),
        )
        if management_atr <= 0 or price <= 0:
            return False
        target = next(
            (item for item in plan.get("take_profits", []) if item["stage"] not in plan.get("completed_stages", [])),
            None,
        )
        if target is None:
            return False
        old_price = _number(target.get("price"))
        new_price = (
            price + 0.5 * management_atr
            if plan["side"] == "long"
            else price - 0.5 * management_atr
        )
        live_side = new_price > price if plan["side"] == "long" else new_price < price
        closer = new_price < old_price if plan["side"] == "long" else new_price > old_price
        if (
            not live_side
            or not closer
            or abs(new_price - old_price) < 0.15 * management_atr
        ):
            return False
        tp_size = planned_take_profit_sizes(
            plan, record.get("current_size", 0)
        ).get(target["stage"])
        if tp_size is None:
            return False
        new_id = await self._replace_trigger(
            plan,
            target["stage"],
            new_price,
            tp_size,
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
            "30m": {
                "atr": context.get("atr30"),
                "recent_low": context.get("recent_low30"),
                "recent_high": context.get("recent_high30"),
            },
            "15m": {"atr": context.get("atr15"), "recent_low": context.get("recent_low15"), "recent_high": context.get("recent_high15")},
            "5m": {"atr": context.get("atr5")},
            "volatility_72h": context.get("volatility_72h", {}),
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

    def _market_driver_contracts(self) -> set[str]:
        return {item.strip().upper() for item in str(self.settings.market_driver_contracts).split(",") if item.strip()}

    @staticmethod
    def _stop_is_better(side: str, new: float, old: float) -> bool:
        if old <= 0 or new <= 0:
            return False
        return new > old if side == "long" else new < old

    def _managed_payload(
        self, position_key: str, plan: dict[str, Any], size: float, entry_response: dict[str, Any], leverage: float
    ) -> dict[str, Any]:
        now = _now()
        opened_at = str(plan.get("opened_at") or now.isoformat())
        horizon_hours = self._time_stop_horizon_hours(plan, self.settings)
        plan["opened_at"] = opened_at
        plan.setdefault("last_trend_confirmed_at", opened_at)
        plan.setdefault("trend_confirmations", 1)
        plan.setdefault(
            "time_stop_deadline",
            (now + timedelta(hours=horizon_hours)).isoformat(),
        )
        return {
            "position_key": position_key,
            "contract": plan["contract"],
            "side": plan["side"],
            "status": "active",
            "current_size": size,
            "leverage": leverage,
            "entry_response": entry_response,
            "plan": plan,
            "updated_at": now,
        }

    async def _notify_order(self, action: dict[str, Any]) -> None:
        if self.notifier and hasattr(self.notifier, "send_order"):
            await self.notifier.send_order(action)

    async def _notify_decisions(self, actions: list[dict[str, Any]]) -> None:
        """Deliver every entry decision without letting Discord block trading."""
        for action in actions:
            try:
                await self._notify_order(action)
            except Exception as exc:
                logger.warning(
                    "Discord decision notification failed for %s: %s",
                    action.get("contract"),
                    type(exc).__name__,
                )

    @staticmethod
    def _time_stop_horizon_hours(plan: dict[str, Any], settings: Any | None) -> float:
        state = str(plan.get("market_state", "normal")).lower()
        if settings is None:
            values = {"trend": 18.0, "range": 6.0, "extreme": 4.0, "base": 12.0}
        else:
            values = {
                "trend": float(settings.time_stop_trend_hours),
                "range": float(settings.time_stop_range_hours),
                "extreme": float(settings.time_stop_extreme_hours),
                "base": float(settings.time_stop_base_hours),
            }
        if state in {"bullish", "bearish"}:
            horizon = values["trend"]
        elif state in {"range", "low_volatility"}:
            horizon = values["range"]
        elif state in {"extreme", "high_volatility"}:
            horizon = values["extreme"]
        else:
            horizon = values["base"]
        profile = plan.get("volatility_profile", {})
        volatility_regime = (
            str(profile.get("regime", "normal"))
            if isinstance(profile, dict)
            else "normal"
        )
        # A persistent expansion needs more development time than an ordinary
        # candle; compression and isolated-event regimes receive less idle
        # capital. Exit still additionally requires insufficient R progress.
        if volatility_regime == "expansion":
            horizon *= 1.25
        elif volatility_regime in {"compression", "isolated_spike"}:
            horizon *= 0.75
        maximum = (
            float(settings.time_stop_max_holding_hours)
            if settings is not None
            else 72.0
        )
        return min(maximum, max(3.0, horizon))

    @staticmethod
    def _as_utc_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            result = value
        elif value:
            try:
                result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import exchange_calendars as xcals
import pandas as pd


def _contract_type(contract_info: Any) -> str:
    raw = getattr(contract_info, "raw", {}) or {}
    return str(raw.get("contract_type") or "").strip().lower()


def stock_calendar_overrides(value: str | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in str(value or "").split(","):
        contract, separator, calendar = item.strip().partition(":")
        if separator and contract.strip() and calendar.strip():
            result[contract.strip().upper()] = calendar.strip().upper()
    return result


@lru_cache(maxsize=32)
def _calendar(name: str) -> Any:
    return xcals.get_calendar(name)


def market_session_status(
    contract_info: Any,
    settings: Any,
    *,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Return the underlying exchange session gate for a stock derivative.

    Gate stock perpetuals can trade around the clock, but a 24h derivative
    quote is not evidence that the underlying equity's primary market is open.
    Every stock defaults to the US regular session and can be mapped to another
    exchange_calendars code with STOCK_CALENDAR_OVERRIDES.
    """
    contract = str(getattr(contract_info, "name", "") or "").upper()
    contract_type = _contract_type(contract_info)
    return contract_session_status(
        contract,
        contract_type,
        settings,
        at=at,
    )


def contract_session_status(
    contract: str,
    contract_type: str,
    settings: Any,
    *,
    at: datetime | None = None,
) -> dict[str, Any]:
    contract = str(contract or "").upper()
    contract_type = str(contract_type or "").lower()
    if contract_type != "stocks":
        return {
            "is_stock": False,
            "is_open": True,
            "calendar": None,
            "reason": "non_stock_contract",
        }

    overrides = stock_calendar_overrides(
        getattr(settings, "stock_calendar_overrides", "")
    )
    calendar_name = overrides.get(
        contract,
        str(getattr(settings, "stock_default_calendar", "XNYS")).upper(),
    )
    reference = at or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    minute = pd.Timestamp(reference.astimezone(timezone.utc)).floor("min")
    try:
        calendar = _calendar(calendar_name)
        is_open = bool(calendar.is_open_on_minute(minute, ignore_breaks=False))
        next_open = None
        if not is_open:
            try:
                next_open = calendar.next_open(minute).isoformat()
            except (ValueError, IndexError):
                next_open = None
        return {
            "is_stock": True,
            "is_open": is_open,
            "calendar": calendar_name,
            "checked_at": reference.astimezone(timezone.utc).isoformat(),
            "next_open": next_open,
            "reason": (
                "underlying_regular_session_open"
                if is_open
                else "underlying_market_closed"
            ),
        }
    except (KeyError, ValueError, TypeError) as exc:
        # Unknown or out-of-range calendars fail closed. A configuration
        # mistake must not silently restore 24-hour stock entries.
        return {
            "is_stock": True,
            "is_open": False,
            "calendar": calendar_name,
            "checked_at": reference.astimezone(timezone.utc).isoformat(),
            "next_open": None,
            "reason": "stock_calendar_unavailable",
            "error_type": type(exc).__name__,
        }

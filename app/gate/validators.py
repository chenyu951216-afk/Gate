from collections.abc import Mapping
from typing import Any, cast

from app.exceptions import SchemaValidationError


def ensure_list(payload: Any, endpoint: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise SchemaValidationError(f"Gate response must be a list: {endpoint}", endpoint=endpoint)
    return [dict(cast(Mapping[str, Any], item)) for item in payload if isinstance(item, Mapping)]


def require_fields(item: Mapping[str, Any], fields: tuple[str, ...], endpoint: str) -> None:
    missing = [field for field in fields if field not in item]
    if missing:
        raise SchemaValidationError(
            f"Gate response missing required fields: {','.join(missing)}", endpoint=endpoint
        )


def number(value: Any, field: str = "value", allow_none: bool = True) -> float | None:
    if value is None and allow_none:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SchemaValidationError(f"Invalid numeric field: {field}") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise SchemaValidationError(f"Non-finite numeric field: {field}")
    return result

from collections.abc import Awaitable, Callable
from typing import Any


async def collect_offset_pages(
    fetch_page: Callable[[int, int], Awaitable[list[dict[str, Any]]]],
    page_size: int = 1000,
    max_pages: int = 100,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for page in range(max_pages):
        page_rows = await fetch_page(page * page_size, page_size)
        rows.extend(page_rows)
        if len(page_rows) < page_size:
            break
    return rows


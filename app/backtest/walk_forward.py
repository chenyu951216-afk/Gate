from typing import Any


def split_walk_forward(rows: list[Any], train_ratio: float = 0.7) -> tuple[list[Any], list[Any]]:
    cut = max(1, min(len(rows) - 1, int(len(rows) * train_ratio))) if len(rows) > 1 else len(rows)
    return rows[:cut], rows[cut:]


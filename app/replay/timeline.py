from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.exceptions import TimeAlignmentError


def parse_time(value: str | datetime, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(ZoneInfo("UTC"))


def align_time(value: datetime, mode: str, interval_minutes: int) -> datetime:
    if interval_minutes <= 0 or 1440 % interval_minutes:
        raise TimeAlignmentError("interval_minutes must divide one day")
    epoch = int(value.timestamp())
    step = interval_minutes * 60
    remainder = epoch % step
    if remainder == 0:
        return value
    if mode == "up":
        return value + timedelta(seconds=step - remainder)
    if mode == "down":
        return value - timedelta(seconds=remainder)
    raise TimeAlignmentError("requested time is not on an interval boundary")


def build_timeline(
    start_time: str | datetime,
    end_time: str | datetime,
    timezone_name: str = "Asia/Taipei",
    interval_minutes: int = 30,
    align_mode: str = "down",
    include_end: bool = True,
) -> list[datetime]:
    start = align_time(parse_time(start_time, timezone_name), align_mode, interval_minutes)
    end = align_time(parse_time(end_time, timezone_name), align_mode, interval_minutes)
    if end < start:
        raise TimeAlignmentError("end_time must not be earlier than start_time")
    step = timedelta(minutes=interval_minutes)
    result: list[datetime] = []
    point = start
    while point < end or (include_end and point == end):
        result.append(point)
        point += step
    return result

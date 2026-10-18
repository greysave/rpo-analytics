"""Timestamp parsing and timezone-aware day bucketing.

All stored timestamps are UTC with one-second resolution. Reports bucket by
*local* calendar day in a caller-chosen IANA timezone, so a day can be 23, 24
or 25 hours long across a DST change.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_timestamp(value: object) -> datetime:
    """Parse an ISO-8601 timestamp that carries an offset or ``Z`` into UTC."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset or 'Z'")
    return parsed.astimezone(UTC).replace(microsecond=0)


def format_timestamp(value: datetime) -> str:
    """Render an aware datetime as the canonical sortable UTC string."""
    if value.tzinfo is None:
        raise ValueError("naive datetime not allowed")
    return value.astimezone(UTC).strftime(TS_FORMAT)


_UTC_NAMES = frozenset({"utc", "etc/utc"})


def get_zone(name: str) -> tzinfo:
    """Resolve an IANA zone name.

    ``UTC`` always works, even when no timezone database is installed. Any other
    zone needs the system tz database or the ``tzdata`` package.
    """
    if name.strip().lower() in _UTC_NAMES:
        return UTC
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(
            f"unknown timezone: {name!r} (not an IANA name, or no timezone database is "
            "available; install the 'tzdata' package)"
        ) from exc
    except ValueError as exc:
        raise ValueError(f"unknown timezone: {name!r}") from exc


def local_day(value: datetime, tz: tzinfo) -> date:
    return value.astimezone(tz).date()


def day_start_utc(day: date, tz: tzinfo) -> datetime:
    """UTC instant at which local calendar ``day`` begins."""
    return datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)


def split_by_local_day(
    start: datetime, end: datetime, tz: tzinfo
) -> list[tuple[date, float]]:
    """Split ``[start, end)`` into per-local-day second counts."""
    out: list[tuple[date, float]] = []
    cur = start
    while cur < end:
        day = local_day(cur, tz)
        boundary = day_start_utc(day + timedelta(days=1), tz)
        if boundary <= cur:  # defensive: skipped-midnight zones
            boundary = cur + timedelta(hours=1)
        seg_end = min(boundary, end)
        out.append((day, (seg_end - cur).total_seconds()))
        cur = seg_end
    return out


def days_in_window(start: datetime, end: datetime, tz: tzinfo) -> list[date]:
    """Local calendar days touched by ``[start, end)``."""
    if end <= start:
        return []
    first = local_day(start, tz)
    last = local_day(end - timedelta(seconds=1), tz)
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]

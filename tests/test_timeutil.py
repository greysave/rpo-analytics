from datetime import UTC, date, datetime, timedelta

import pytest

from rpo_analytics.timeutil import (
    days_in_window,
    format_timestamp,
    get_zone,
    local_day,
    parse_timestamp,
    split_by_local_day,
)


def test_parse_normalises_offsets_to_utc() -> None:
    assert parse_timestamp("2026-03-01T09:00:00+09:00") == datetime(
        2026, 3, 1, 0, 0, tzinfo=UTC
    )
    assert format_timestamp(parse_timestamp("2026-03-01T00:00:00z")) == (
        "2026-03-01T00:00:00Z"
    )


@pytest.mark.parametrize("bad", ["2026-03-01T00:00:00", "nope", 5, ""])
def test_parse_rejects_naive_or_garbage(bad: object) -> None:
    with pytest.raises(ValueError):
        parse_timestamp(bad)


def test_unknown_zone() -> None:
    with pytest.raises(ValueError):
        get_zone("Mars/Olympus")


def test_local_day_crosses_midnight_in_tokyo() -> None:
    instant = datetime(2026, 3, 1, 23, 30, tzinfo=UTC)
    assert local_day(instant, get_zone("UTC")) == date(2026, 3, 1)
    assert local_day(instant, get_zone("Asia/Tokyo")) == date(2026, 3, 2)
    assert local_day(instant, get_zone("America/Los_Angeles")) == date(2026, 3, 1)


def test_split_exactly_on_midnight_boundary() -> None:
    tz = get_zone("UTC")
    start = datetime(2026, 3, 1, 23, 0, tzinfo=UTC)
    parts = split_by_local_day(start, start + timedelta(hours=2), tz)
    assert parts == [(date(2026, 3, 1), 3600.0), (date(2026, 3, 2), 3600.0)]


def test_dst_spring_forward_day_is_23_hours() -> None:
    tz = get_zone("America/New_York")  # 2026-03-08 loses an hour
    start = datetime(2026, 3, 8, 0, 0, tzinfo=tz).astimezone(UTC)
    end = datetime(2026, 3, 9, 0, 0, tzinfo=tz).astimezone(UTC)
    assert split_by_local_day(start, end, tz) == [(date(2026, 3, 8), 23 * 3600.0)]


def test_dst_fall_back_day_is_25_hours() -> None:
    tz = get_zone("America/New_York")  # 2026-11-01 gains an hour
    start = datetime(2026, 11, 1, 0, 0, tzinfo=tz).astimezone(UTC)
    end = datetime(2026, 11, 2, 0, 0, tzinfo=tz).astimezone(UTC)
    assert split_by_local_day(start, end, tz) == [(date(2026, 11, 1), 25 * 3600.0)]


def test_days_in_window_end_is_exclusive() -> None:
    tz = get_zone("UTC")
    start = datetime(2026, 3, 1, tzinfo=UTC)
    assert days_in_window(start, start + timedelta(days=2), tz) == [
        date(2026, 3, 1),
        date(2026, 3, 2),
    ]
    assert days_in_window(start, start, tz) == []


def test_utc_works_without_any_tz_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from zoneinfo import ZoneInfoNotFoundError

    from rpo_analytics import timeutil

    def missing(_key: str) -> object:
        raise ZoneInfoNotFoundError("no tz database")

    monkeypatch.setattr(timeutil, "ZoneInfo", missing)
    for name in ("UTC", "utc", "Etc/UTC", " UTC "):
        assert get_zone(name) is UTC
    instant = datetime(2026, 3, 1, 23, 30, tzinfo=UTC)
    assert local_day(instant, get_zone("UTC")) == date(2026, 3, 1)
    with pytest.raises(ValueError, match="install the 'tzdata' package"):
        get_zone("America/New_York")


def test_unknown_zone_is_value_error_with_hint() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        get_zone("Mars/Olympus")
    with pytest.raises(ValueError, match="unknown timezone"):
        get_zone("../../etc/passwd")

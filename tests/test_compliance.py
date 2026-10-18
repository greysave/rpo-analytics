from datetime import date, datetime, timedelta
from typing import Any

import pytest

from rpo_analytics.analytics import Window, compute_compliance, resolve_window
from rpo_analytics.timeutil import get_zone
from tests.helpers import T0, load, make_db, run

UTC_TZ = get_zone("UTC")


def _every(
    start: datetime, end: datetime, minutes: int, prefix: str
) -> list[dict[str, Any]]:
    out = []
    cur = start
    n = 0
    while cur <= end:
        out.append(run(f"{prefix}{n}", cur))
        cur += timedelta(minutes=minutes)
        n += 1
    return out


def test_compliant_object() -> None:
    conn = make_db()
    load(conn, _every(T0, T0 + timedelta(days=1), 30, "a"))
    report = compute_compliance(conn, Window(T0, T0 + timedelta(days=1), UTC_TZ))
    (obj,) = report.objects
    assert obj.status == "compliant"
    assert obj.violation_seconds == 0
    assert obj.compliance_ratio == 1.0
    assert report.overall_ratio == 1.0


def test_mid_window_gap_is_counted_beyond_the_rpo() -> None:
    conn = make_db()
    runs = _every(T0, T0 + timedelta(hours=5), 30, "a")
    runs += _every(T0 + timedelta(hours=7), T0 + timedelta(days=1), 30, "b")
    load(conn, runs)
    (obj,) = compute_compliance(
        conn, Window(T0, T0 + timedelta(days=1), UTC_TZ)
    ).objects
    # 05:00 -> 07:00 is a 120 min gap against a 60 min RPO: 3600 s violated.
    assert obj.status == "violated"
    assert obj.violation_count == 1
    assert obj.violation_seconds == 3600
    assert obj.longest_gap_minutes == 120
    assert obj.compliance_ratio == pytest.approx(1 - 3600 / obj.observed_seconds)


def test_trailing_outage_runs_to_window_end() -> None:
    conn = make_db()
    load(conn, _every(T0, T0 + timedelta(hours=12), 30, "a"))
    window = Window(T0, T0 + timedelta(days=1), UTC_TZ)
    (obj,) = compute_compliance(conn, window).objects
    # Last point 12:00, RPO expires 13:00, window ends 24:00.
    assert obj.violation_seconds == 11 * 3600
    assert obj.violation_count == 1


def test_gap_exactly_equal_to_rpo_is_compliant() -> None:
    conn = make_db()
    load(conn, _every(T0, T0 + timedelta(hours=6), 60, "a"))
    window = Window(T0, T0 + timedelta(hours=6), UTC_TZ)
    (obj,) = compute_compliance(conn, window).objects
    assert obj.status == "compliant"


def test_missing_data_only_failures_means_violated_with_no_recovery_points() -> None:
    conn = make_db()
    load(conn, [run("f1", T0 + timedelta(hours=1), status="failed")])
    window = Window(T0, T0 + timedelta(hours=10), UTC_TZ)
    (obj,) = compute_compliance(conn, window).objects
    assert obj.recovery_points == 0
    assert obj.status == "violated"
    # first seen 00:59, RPO to 01:59, violated until 10:00
    assert obj.violation_seconds == (10 * 3600) - (1 * 3600 + 59 * 60)


def test_prior_recovery_point_before_window_carries_in() -> None:
    conn = make_db()
    load(conn, [run("p", T0 - timedelta(hours=3))])
    window = Window(T0, T0 + timedelta(hours=2), UTC_TZ)
    (obj,) = compute_compliance(conn, window).objects
    # Point at -03:00 expires at -02:00; the whole window is out of compliance.
    assert obj.violation_seconds == 2 * 3600
    assert obj.compliance_ratio == 0.0


def test_object_first_seen_mid_window_is_not_penalised_before_it_existed() -> None:
    conn = make_db()
    load(conn, _every(T0 + timedelta(hours=20), T0 + timedelta(days=1), 30, "n"))
    window = Window(T0, T0 + timedelta(days=1), UTC_TZ)
    (obj,) = compute_compliance(conn, window).objects
    assert obj.status == "compliant"
    assert obj.observed_seconds < 5 * 3600


def test_object_first_seen_after_window_is_omitted() -> None:
    conn = make_db()
    load(conn, [run("late", T0 + timedelta(days=3))])
    window = Window(T0, T0 + timedelta(days=1), UTC_TZ)
    assert compute_compliance(conn, window).objects == []


def test_daily_violation_buckets_follow_timezone() -> None:
    conn = make_db()
    runs = [run("a", T0 + timedelta(hours=22, minutes=59))]
    runs += [run("b", T0 + timedelta(hours=23))]
    runs += _every(T0 + timedelta(hours=27), T0 + timedelta(hours=27), 30, "c")
    load(conn, runs)
    window = Window(T0, T0 + timedelta(hours=27), UTC_TZ)
    # 23:00 point expires at 00:00 Mar 2 UTC; next point at 03:00 Mar 2 UTC.
    (utc,) = compute_compliance(conn, window).objects
    assert utc.daily_violation_seconds == {date(2026, 3, 2): 10800.0}
    la = Window(window.start, window.end, get_zone("America/Los_Angeles"))
    (obj_la,) = compute_compliance(conn, la).objects
    assert obj_la.daily_violation_seconds == {date(2026, 3, 1): 10800.0}


def test_violation_spanning_midnight_is_split_across_days() -> None:
    conn = make_db()
    load(conn, [run("a", T0 + timedelta(hours=21)), run("b", T0 + timedelta(hours=27))])
    window = Window(T0, T0 + timedelta(hours=27), UTC_TZ)
    (obj,) = compute_compliance(conn, window).objects
    # Expires 22:00; next point 03:00 next day: 2 h on Mar 1, 3 h on Mar 2.
    assert obj.daily_violation_seconds == {
        date(2026, 3, 1): 2 * 3600.0,
        date(2026, 3, 2): 3 * 3600.0,
    }
    assert sum(obj.daily_violation_seconds.values()) == obj.violation_seconds


def test_policy_comes_from_latest_run() -> None:
    conn = make_db()
    load(conn, _every(T0, T0 + timedelta(hours=4), 90, "a"), rpo=60)
    (obj,) = compute_compliance(
        conn, Window(T0, T0 + timedelta(hours=4), UTC_TZ)
    ).objects
    assert obj.rpo_minutes == 60
    assert obj.status == "violated"
    load(conn, [], rpo=120)  # policy update applies to all objects
    (obj,) = compute_compliance(
        conn, Window(T0, T0 + timedelta(hours=4), UTC_TZ)
    ).objects
    assert obj.status == "compliant"


def test_resolve_window_defaults_to_newest_run_and_handles_empty() -> None:
    conn = make_db()
    assert resolve_window(conn, UTC_TZ) is None
    load(conn, [run("a", T0 + timedelta(hours=1))])
    window = resolve_window(conn, UTC_TZ, days=2)
    assert window is not None
    assert window.end == T0 + timedelta(hours=1)
    assert window.start == window.end - timedelta(days=2)
    with pytest.raises(ValueError):
        resolve_window(conn, UTC_TZ, days=0)

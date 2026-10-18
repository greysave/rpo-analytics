from datetime import date, timedelta
from typing import Any

import pytest

from rpo_analytics.analytics import (
    DailyStat,
    DriftConfig,
    Window,
    compute_daily_stats,
    detect_drift,
    rolling_trend,
)
from rpo_analytics.timeutil import get_zone
from tests.helpers import T0, load, make_db, run

UTC_TZ = get_zone("UTC")
DAY0 = date(2026, 3, 1)


def stat(i: int, runs: int, ok: int, gap: float | None) -> DailyStat:
    return DailyStat(DAY0 + timedelta(days=i), runs, ok, runs - ok, 0, gap)


def _series(
    start_day: int, end_day: int, minutes: int, prefix: str, status: str = "success"
) -> list[dict[str, Any]]:
    out = []
    cur = T0 + timedelta(days=start_day)
    stop = T0 + timedelta(days=end_day)
    n = 0
    while cur < stop:
        out.append(run(f"{prefix}{n}", cur, status=status))
        cur += timedelta(minutes=minutes)
        n += 1
    return out


def test_daily_stats_counts_and_missing_days() -> None:
    conn = make_db()
    runs = [
        run("a", T0 + timedelta(hours=1)),
        run("b", T0 + timedelta(hours=3), status="failed"),
        run("c", T0 + timedelta(hours=5), status="warning", stored=50),
        run("d", T0 + timedelta(days=2, hours=1), stored=10),
    ]
    load(conn, runs)
    window = Window(T0, T0 + timedelta(days=3), UTC_TZ)
    stats = compute_daily_stats(conn, window)["vm-001"]
    assert [s.day for s in stats] == [DAY0, date(2026, 3, 2), date(2026, 3, 3)]
    day1, day2, day3 = stats
    assert (day1.runs, day1.recovery_points, day1.failures) == (3, 2, 1)
    assert day1.written_bytes == 50
    assert day1.max_gap_minutes == 240  # 01:00 -> 05:00 (failed run is no point)
    assert day2.missing and day2.success_rate is None and day2.max_gap_minutes is None
    assert (
        day3.max_gap_minutes
        == (
            (T0 + timedelta(days=2, hours=1)) - (T0 + timedelta(hours=5))
        ).total_seconds()
        / 60
    )


def test_rolling_trend_pools_rates_and_skips_missing_days() -> None:
    stats = [
        stat(0, 10, 10, 30.0),
        stat(1, 10, 5, 60.0),
        stat(2, 0, 0, None),
        stat(3, 10, 10, 90.0),
    ]
    pts = rolling_trend(stats, 3)
    assert pts[0].rolling_success_rate == 1.0
    assert pts[1].rolling_success_rate == pytest.approx(15 / 20)
    assert pts[1].rolling_max_gap_minutes == pytest.approx(45.0)
    # day 2 window = days 0..2: missing day adds nothing
    assert pts[2].rolling_success_rate == pytest.approx(15 / 20)
    assert pts[2].missing is True
    # day 3 window = days 1..3
    assert pts[3].rolling_success_rate == pytest.approx(15 / 20)
    assert pts[3].rolling_max_gap_minutes == pytest.approx(75.0)
    with pytest.raises(ValueError):
        rolling_trend(stats, 0)


def test_rolling_trend_all_missing_window_is_none() -> None:
    pts = rolling_trend([stat(0, 0, 0, None), stat(1, 0, 0, None)], 2)
    assert pts[1].rolling_success_rate is None
    assert pts[1].rolling_max_gap_minutes is None


def _drift_stats(baseline_gap: float, recent_gap: float) -> list[DailyStat]:
    return [stat(i, 48, 48, baseline_gap if i < 23 else recent_gap) for i in range(30)]


def test_drift_warning_when_gap_grows_but_stays_inside_rpo() -> None:
    alerts = detect_drift("vm-1", _drift_stats(30, 50), rpo_minutes=60)
    (alert,) = alerts
    assert (alert.kind, alert.severity) == ("rpo_drift", "warning")
    assert alert.change_pct == pytest.approx(66.666, rel=1e-3)


def test_drift_critical_when_recent_gap_exceeds_rpo() -> None:
    (alert,) = detect_drift("vm-1", _drift_stats(30, 50), rpo_minutes=40)
    assert alert.severity == "critical"


def test_no_drift_when_stable_or_below_threshold() -> None:
    assert detect_drift("vm-1", _drift_stats(30, 30), 60) == []
    assert detect_drift("vm-1", _drift_stats(30, 37), 60) == []  # +23% < 25%
    assert detect_drift("vm-1", _drift_stats(30, 20), 60) == []  # improvement


def test_drift_threshold_is_configurable() -> None:
    cfg = DriftConfig(gap_increase_pct=10.0)
    assert len(detect_drift("vm-1", _drift_stats(30, 37), 60, cfg)) == 1


def test_no_recent_data_alert() -> None:
    stats = [
        stat(i, 48, 48, 30.0) if i < 23 else stat(i, 0, 0, None) for i in range(30)
    ]
    (alert,) = detect_drift("vm-1", stats, 60)
    assert (alert.kind, alert.severity) == ("no_recent_data", "critical")


def test_success_rate_drop_alert() -> None:
    stats = [stat(i, 10, 10 if i < 23 else 6, 30.0) for i in range(30)]
    (alert,) = detect_drift("vm-1", stats, 60)
    assert alert.kind == "success_rate_drop"
    assert alert.baseline == 1.0 and alert.recent == pytest.approx(0.6)


def test_insufficient_history_is_info_only() -> None:
    (alert,) = detect_drift("vm-1", [stat(i, 5, 5, 30.0) for i in range(10)], 60)
    assert (alert.kind, alert.severity) == ("insufficient_history", "info")


def test_too_few_gap_samples_suppress_gap_drift() -> None:
    stats = [stat(i, 5, 5, 30.0 if i < 2 else None) for i in range(23)]
    stats += [stat(i, 5, 5, 200.0) for i in range(23, 30)]
    assert detect_drift("vm-1", stats, 60) == []


def test_end_to_end_drift_from_database() -> None:
    conn = make_db()
    runs = _series(0, 23, 30, "a") + _series(23, 30, 50, "b")
    load(conn, runs)
    window = Window(T0, T0 + timedelta(days=30), UTC_TZ)
    stats = compute_daily_stats(conn, window)["vm-001"]
    alerts = detect_drift("vm-001", stats, 60)
    assert [a.kind for a in alerts] == ["rpo_drift"]
    assert alerts[0].recent == pytest.approx(50.0)
    assert alerts[0].baseline == pytest.approx(30.0, abs=0.5)

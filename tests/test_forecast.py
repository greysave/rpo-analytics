from datetime import date, timedelta

import pytest

from rpo_analytics.analytics import (
    Window,
    cumulative_written_bytes,
    forecast_capacity,
    linear_fit,
)
from rpo_analytics.timeutil import get_zone
from tests.helpers import T0, load, make_db, run

D0 = date(2026, 3, 1)


def line(n: int, slope: int, base: int) -> list[tuple[date, int]]:
    return [(D0 + timedelta(days=i), base + slope * i) for i in range(n)]


def test_linear_fit_exact_line() -> None:
    slope, intercept, r2 = linear_fit([0, 1, 2, 3], [5, 7, 9, 11])
    assert (slope, intercept, r2) == (2.0, 5.0, 1.0)


def test_linear_fit_known_noisy_values() -> None:
    # hand-computed: mx=1.5 my=3.5 sxx=5 sxy=4 -> slope .8, intercept 2.3,
    # SS_res=1.8, SS_tot=5
    slope, intercept, r2 = linear_fit([0, 1, 2, 3], [2, 4, 3, 5])
    assert slope == pytest.approx(0.8)
    assert intercept == pytest.approx(2.3)
    assert r2 == pytest.approx(1 - 1.8 / 5.0)


def test_linear_fit_constant_series_has_zero_slope_and_r2_one() -> None:
    assert linear_fit([0, 1, 2], [4, 4, 4]) == (0.0, 4.0, 1.0)


def test_linear_fit_validation() -> None:
    with pytest.raises(ValueError):
        linear_fit([1.0], [1.0])
    with pytest.raises(ValueError):
        linear_fit([1.0, 1.0], [1.0, 2.0])


def test_forecast_days_until_full_math() -> None:
    fc = forecast_capacity(line(10, 100, 1000), capacity_limit_bytes=3000)
    assert fc.slope_bytes_per_day == pytest.approx(100)
    assert fc.r_squared == pytest.approx(1.0)
    assert fc.last_bytes == 1900
    assert fc.days_until_full == pytest.approx(11.0)  # (3000-1900)/100
    assert fc.projected_full_date == D0 + timedelta(days=9 + 11)
    assert [(p.days_ahead, p.bytes) for p in fc.projections] == [
        (30, 4900),
        (60, 7900),
        (90, 10900),
    ]


def test_forecast_rounds_partial_days_up() -> None:
    fc = forecast_capacity(line(5, 100, 0), capacity_limit_bytes=450 + 400)
    assert fc.days_until_full == pytest.approx(4.5)
    assert fc.projected_full_date == D0 + timedelta(days=4 + 5)


def test_forecast_already_full_and_flat_growth() -> None:
    full = forecast_capacity(line(5, 100, 0), capacity_limit_bytes=100)
    assert full.days_until_full == 0.0
    assert full.projected_full_date == D0 + timedelta(days=4)
    flat = forecast_capacity(line(5, 0, 500), capacity_limit_bytes=10_000)
    assert flat.days_until_full is None and flat.projected_full_date is None
    shrinking = forecast_capacity(line(5, -10, 500), capacity_limit_bytes=10_000)
    assert shrinking.days_until_full is None


def test_forecast_without_capacity() -> None:
    fc = forecast_capacity(line(3, 10, 0))
    assert fc.days_until_full is None and fc.projected_full_date is None


def test_forecast_needs_two_points() -> None:
    with pytest.raises(ValueError):
        forecast_capacity(line(1, 1, 1))


def test_cumulative_series_includes_history_and_flat_missing_days() -> None:
    conn = make_db()
    runs = [
        run("old", T0 - timedelta(days=5), stored=500),
        run("d0", T0 + timedelta(hours=2), stored=100),
        run("d2", T0 + timedelta(days=2, hours=2), stored=300),
        run("d2b", T0 + timedelta(days=2, hours=3), stored=50, status="failed"),
    ]
    load(conn, runs)
    window = Window(T0, T0 + timedelta(days=3), get_zone("UTC"))
    assert cumulative_written_bytes(conn, window) == [
        (D0, 600),
        (D0 + timedelta(days=1), 600),
        (D0 + timedelta(days=2), 950),
    ]
    fc = forecast_capacity(cumulative_written_bytes(conn, window))
    assert fc.slope_bytes_per_day == pytest.approx(175.0)

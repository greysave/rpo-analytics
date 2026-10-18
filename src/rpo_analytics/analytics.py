"""RPO compliance, drift detection, rolling trends and capacity forecast.

Definitions (all deliberately simple and inspectable):

* A *recovery point* is a run with status ``success`` or ``warning``; its
  timestamp is ``ended_at``.
* An object's RPO is the ``rpo_minutes`` of the policy on its most recent run.
* Between consecutive recovery points ``a`` and ``b`` the object is out of
  compliance during ``[a + rpo, b)``. The window end acts as the final ``b``.
* Daily figures are attributed to the local calendar day of ``ended_at``.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from itertools import pairwise

from rpo_analytics.timeutil import (
    days_in_window,
    format_timestamp,
    local_day,
    parse_timestamp,
    split_by_local_day,
)

RECOVERY_STATUSES = ("success", "warning")


@dataclass(frozen=True, slots=True)
class Window:
    start: datetime
    end: datetime
    tz: tzinfo


def resolve_window(
    conn: sqlite3.Connection,
    tz: tzinfo,
    days: int = 30,
    end: datetime | None = None,
) -> Window | None:
    """Window of ``days`` ending at ``end`` (default: newest run end)."""
    if days <= 0:
        raise ValueError("days must be positive")
    if end is None:
        row = conn.execute("SELECT MAX(ended_at) FROM runs").fetchone()
        if row[0] is None:
            return None
        end = parse_timestamp(row[0])
    return Window(end - timedelta(days=days), end, tz)


# ----------------------------------------------------------------- loading


@dataclass(frozen=True, slots=True)
class _ObjectInfo:
    policy_key: str
    rpo_minutes: int
    first_seen: datetime


def _object_info(conn: sqlite3.Connection, end: datetime) -> dict[str, _ObjectInfo]:
    rows = conn.execute(
        "SELECT r.workload_id, r.policy_key, p.rpo_minutes, r.started_at "
        "FROM runs r JOIN policies p USING (policy_key) "
        "WHERE r.started_at <= ? ORDER BY r.started_at, r.run_id",
        (format_timestamp(end),),
    )
    info: dict[str, _ObjectInfo] = {}
    for oid, pid, rpo, started in rows:
        first = info[oid].first_seen if oid in info else parse_timestamp(started)
        info[oid] = _ObjectInfo(pid, int(rpo), first)
    return info


def _recovery_points(
    conn: sqlite3.Connection, end: datetime
) -> dict[str, list[datetime]]:
    rows = conn.execute(
        "SELECT workload_id, ended_at FROM runs "
        "WHERE status IN ('success', 'warning') AND ended_at <= ? "
        "ORDER BY workload_id, ended_at",
        (format_timestamp(end),),
    )
    points: dict[str, list[datetime]] = {}
    for oid, ended in rows:
        points.setdefault(oid, []).append(parse_timestamp(ended))
    return points


# -------------------------------------------------------------- compliance


@dataclass(frozen=True, slots=True)
class ObjectCompliance:
    workload_id: str
    policy_key: str
    rpo_minutes: int
    status: str  # "compliant" | "violated"
    compliance_ratio: float
    violation_count: int
    violation_seconds: float
    longest_gap_minutes: float
    recovery_points: int
    observed_seconds: float
    daily_violation_seconds: dict[date, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ComplianceReport:
    window: Window
    objects: list[ObjectCompliance]

    @property
    def overall_ratio(self) -> float:
        observed = sum(o.observed_seconds for o in self.objects)
        if observed == 0:
            return 1.0
        return 1.0 - sum(o.violation_seconds for o in self.objects) / observed


def compute_compliance(conn: sqlite3.Connection, window: Window) -> ComplianceReport:
    info = _object_info(conn, window.end)
    points = _recovery_points(conn, window.end)
    results: list[ObjectCompliance] = []
    for oid in sorted(info):
        meta = info[oid]
        pts = points.get(oid, [])
        rpo = timedelta(minutes=meta.rpo_minutes)
        prior = [p for p in pts if p < window.start]
        inside = [p for p in pts if p >= window.start]
        if prior:
            anchor = window.start
            chain_start = prior[-1]
        else:
            # Not protected before first sighting: don't penalise earlier time.
            anchor = max(window.start, meta.first_seen)
            chain_start = anchor
        if anchor >= window.end:
            continue
        seq = [chain_start, *inside]
        if seq[-1] < window.end:
            seq.append(window.end)

        longest = timedelta(0)
        violation = 0.0
        count = 0
        daily: dict[date, float] = {}
        for a, b in pairwise(seq):
            longest = max(longest, b - a)
            if b - a <= rpo:
                continue
            v_start = max(a + rpo, window.start)
            v_end = min(b, window.end)
            if v_end <= v_start:
                continue
            count += 1
            for day, secs in split_by_local_day(v_start, v_end, window.tz):
                daily[day] = daily.get(day, 0.0) + secs
                violation += secs
        observed = (window.end - anchor).total_seconds()
        results.append(
            ObjectCompliance(
                workload_id=oid,
                policy_key=meta.policy_key,
                rpo_minutes=meta.rpo_minutes,
                status="violated" if violation > 0 else "compliant",
                compliance_ratio=max(0.0, 1.0 - violation / observed),
                violation_count=count,
                violation_seconds=violation,
                longest_gap_minutes=longest.total_seconds() / 60,
                recovery_points=len(inside),
                observed_seconds=observed,
                daily_violation_seconds=daily,
            )
        )
    return ComplianceReport(window, results)


# ------------------------------------------------------------ daily series


@dataclass(frozen=True, slots=True)
class DailyStat:
    day: date
    runs: int
    recovery_points: int
    failures: int
    written_bytes: int
    max_gap_minutes: float | None

    @property
    def missing(self) -> bool:
        return self.runs == 0

    @property
    def success_rate(self) -> float | None:
        return self.recovery_points / self.runs if self.runs else None


def compute_daily_stats(
    conn: sqlite3.Connection, window: Window
) -> dict[str, list[DailyStat]]:
    """Per-object daily stats; days without runs are present and ``missing``."""
    days = days_in_window(window.start, window.end, window.tz)
    points = _recovery_points(conn, window.end)
    info = _object_info(conn, window.end)

    runs: dict[str, dict[date, list[int]]] = {}  # [runs, ok, failed, stored]
    rows = conn.execute(
        "SELECT workload_id, ended_at, status, written_bytes FROM runs "
        "WHERE ended_at >= ? AND ended_at < ?",
        (format_timestamp(window.start), format_timestamp(window.end)),
    )
    for oid, ended, status, stored in rows:
        day = local_day(parse_timestamp(ended), window.tz)
        slot = runs.setdefault(oid, {}).setdefault(day, [0, 0, 0, 0])
        slot[0] += 1
        slot[1 if status in RECOVERY_STATUSES else 2] += 1
        slot[3] += int(stored)

    out: dict[str, list[DailyStat]] = {}
    for oid in sorted(info):
        gaps: dict[date, float] = {}
        pts = points.get(oid, [])
        for prev, cur in pairwise(pts):
            if cur < window.start:
                continue
            day = local_day(cur, window.tz)
            gap = (cur - prev).total_seconds() / 60
            gaps[day] = max(gaps.get(day, 0.0), gap)
        per_day = runs.get(oid, {})
        series: list[DailyStat] = []
        for day in days:
            n, ok, bad, stored = per_day.get(day, [0, 0, 0, 0])
            series.append(DailyStat(day, n, ok, bad, stored, gaps.get(day)))
        out[oid] = series
    return out


# ----------------------------------------------------------- rolling trend


@dataclass(frozen=True, slots=True)
class TrendPoint:
    day: date
    success_rate: float | None
    max_gap_minutes: float | None
    rolling_success_rate: float | None
    rolling_max_gap_minutes: float | None
    missing: bool


def rolling_trend(stats: Sequence[DailyStat], window_days: int = 7) -> list[TrendPoint]:
    """Trailing-window trend.

    ``rolling_success_rate`` is pooled (sum of recovery points / sum of runs);
    ``rolling_max_gap_minutes`` is the mean of the days that have a gap value.
    Missing days contribute nothing instead of being treated as zero.
    """
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    points: list[TrendPoint] = []
    for i, stat in enumerate(stats):
        chunk = stats[max(0, i - window_days + 1) : i + 1]
        runs = sum(s.runs for s in chunk)
        ok = sum(s.recovery_points for s in chunk)
        gaps = [s.max_gap_minutes for s in chunk if s.max_gap_minutes is not None]
        points.append(
            TrendPoint(
                day=stat.day,
                success_rate=stat.success_rate,
                max_gap_minutes=stat.max_gap_minutes,
                rolling_success_rate=ok / runs if runs else None,
                rolling_max_gap_minutes=sum(gaps) / len(gaps) if gaps else None,
                missing=stat.missing,
            )
        )
    return points


# ------------------------------------------------------------------- drift


@dataclass(frozen=True, slots=True)
class DriftConfig:
    baseline_days: int = 14
    recent_days: int = 7
    gap_increase_pct: float = 25.0
    success_drop_points: float = 0.15
    min_gap_samples: int = 3


@dataclass(frozen=True, slots=True)
class DriftAlert:
    workload_id: str
    kind: str  # rpo_drift | success_rate_drop | no_recent_data | insufficient_history
    severity: str  # info | warning | critical
    message: str
    baseline: float | None = None
    recent: float | None = None
    change_pct: float | None = None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def detect_drift(
    workload_id: str,
    stats: Sequence[DailyStat],
    rpo_minutes: int,
    config: DriftConfig | None = None,
) -> list[DriftAlert]:
    """Compare the first ``baseline_days`` with the last ``recent_days``."""
    config = config or DriftConfig()
    need = config.baseline_days + config.recent_days
    if len(stats) < need:
        return [
            DriftAlert(
                workload_id,
                "insufficient_history",
                "info",
                f"{len(stats)} days available, {need} needed",
            )
        ]
    base = stats[: config.baseline_days]
    recent = stats[-config.recent_days :]
    alerts: list[DriftAlert] = []

    if all(s.missing for s in recent):
        alerts.append(
            DriftAlert(
                workload_id,
                "no_recent_data",
                "critical",
                f"no runs in the last {config.recent_days} days",
            )
        )
        return alerts

    base_gaps = [s.max_gap_minutes for s in base if s.max_gap_minutes is not None]
    recent_gaps = [s.max_gap_minutes for s in recent if s.max_gap_minutes is not None]
    b_gap, r_gap = _mean(base_gaps), _mean(recent_gaps)
    if (
        b_gap
        and r_gap is not None
        and len(base_gaps) >= config.min_gap_samples
        and len(recent_gaps) >= 1
    ):
        pct = (r_gap - b_gap) / b_gap * 100
        if pct > config.gap_increase_pct:
            sev = "critical" if r_gap > rpo_minutes else "warning"
            alerts.append(
                DriftAlert(
                    workload_id,
                    "rpo_drift",
                    sev,
                    f"mean daily max gap {b_gap:.1f} -> {r_gap:.1f} min "
                    f"(+{pct:.1f}%, RPO {rpo_minutes} min)",
                    b_gap,
                    r_gap,
                    pct,
                )
            )

    b_runs, r_runs = sum(s.runs for s in base), sum(s.runs for s in recent)
    if b_runs and r_runs:
        b_rate = sum(s.recovery_points for s in base) / b_runs
        r_rate = sum(s.recovery_points for s in recent) / r_runs
        if b_rate - r_rate >= config.success_drop_points:
            alerts.append(
                DriftAlert(
                    workload_id,
                    "success_rate_drop",
                    "warning",
                    f"success rate {b_rate:.1%} -> {r_rate:.1%}",
                    b_rate,
                    r_rate,
                    (r_rate - b_rate) / b_rate * 100,
                )
            )
    return alerts


# ---------------------------------------------------------------- forecast


@dataclass(frozen=True, slots=True)
class ProjectionPoint:
    days_ahead: int
    day: date
    bytes: int


@dataclass(frozen=True, slots=True)
class CapacityForecast:
    n_points: int
    slope_bytes_per_day: float
    intercept_bytes: float
    r_squared: float
    last_day: date
    last_bytes: int
    days_until_full: float | None
    projected_full_date: date | None
    projections: list[ProjectionPoint]


def cumulative_written_bytes(
    conn: sqlite3.Connection, window: Window
) -> list[tuple[date, int]]:
    """Total stored bytes (all objects) at the end of each local day."""
    base_row = conn.execute(
        "SELECT COALESCE(SUM(written_bytes), 0) FROM runs WHERE ended_at < ?",
        (format_timestamp(window.start),),
    ).fetchone()
    total = int(base_row[0])
    per_day: dict[date, int] = {}
    rows = conn.execute(
        "SELECT ended_at, written_bytes FROM runs WHERE ended_at >= ? AND ended_at < ?",
        (format_timestamp(window.start), format_timestamp(window.end)),
    )
    for ended, stored in rows:
        day = local_day(parse_timestamp(ended), window.tz)
        per_day[day] = per_day.get(day, 0) + int(stored)
    series: list[tuple[date, int]] = []
    for day in days_in_window(window.start, window.end, window.tz):
        total += per_day.get(day, 0)
        series.append((day, total))
    return series


def linear_fit(xs: Sequence[float], ys: Sequence[float]) -> tuple[float, float, float]:
    """Ordinary least squares: returns (slope, intercept, r_squared).

    slope = sum((x-mx)(y-my)) / sum((x-mx)^2); intercept = my - slope*mx;
    r^2 = 1 - SS_res / SS_tot (defined as 1.0 when y is constant).
    """
    n = len(xs)
    if n < 2 or n != len(ys):
        raise ValueError("need at least two paired points")
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        raise ValueError("x values must not all be equal")
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum(
        (y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True)
    )
    r2 = 1.0 if ss_tot == 0 else 1.0 - ss_res / ss_tot
    return slope, intercept, r2


def forecast_capacity(
    series: Sequence[tuple[date, int]],
    capacity_limit_bytes: int | None = None,
    horizons: Sequence[int] = (30, 60, 90),
) -> CapacityForecast:
    """Straight-line projection of cumulative stored bytes.

    Not a model of retention, dedupe or seasonality: it extends the fitted line
    from the last observed value, and ``days_until_full`` is
    ``(capacity - last_bytes) / slope`` (``None`` if the slope is not positive).
    """
    if len(series) < 2:
        raise ValueError("need at least two days of data to forecast")
    first = series[0][0]
    xs = [float((d - first).days) for d, _ in series]
    ys = [float(v) for _, v in series]
    slope, intercept, r2 = linear_fit(xs, ys)
    last_day, last_bytes = series[-1]

    days_left: float | None = None
    full_date: date | None = None
    if capacity_limit_bytes is not None and slope > 0:
        days_left = max(0.0, (capacity_limit_bytes - last_bytes) / slope)
        full_date = last_day + timedelta(days=math.ceil(days_left))
    projections = [
        ProjectionPoint(h, last_day + timedelta(days=h), round(last_bytes + slope * h))
        for h in horizons
    ]
    return CapacityForecast(
        len(series),
        slope,
        intercept,
        r2,
        last_day,
        last_bytes,
        days_left,
        full_date,
        projections,
    )

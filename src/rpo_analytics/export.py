"""Row builders and CSV / JSON / table rendering for reports."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import date, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from rpo_analytics.analytics import (
    CapacityForecast,
    ComplianceReport,
    DriftAlert,
    TrendPoint,
)
from rpo_analytics.timeutil import format_timestamp

Row = dict[str, Any]


def _plain(obj: object) -> Any:
    """Recursively convert reports to JSON-safe values (dates -> ISO strings)."""
    if isinstance(obj, datetime):
        return format_timestamp(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Mapping):
        return {str(_plain(k)): _plain(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_plain(v) for v in obj]
    if obj is None or isinstance(obj, str | int | float | bool):
        return obj
    if isinstance(obj, ZoneInfo):
        return obj.key
    if isinstance(obj, tzinfo):
        return obj.tzname(None) or "UTC"
    raise TypeError(f"not serialisable: {type(obj).__name__}")


def to_json(payload: object) -> str:
    return json.dumps(_plain(payload), indent=2, sort_keys=True) + "\n"


def _cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".") or "0"
    if isinstance(value, datetime):
        return format_timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def to_csv(rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(row.get(k)) for k in fieldnames})
    return buf.getvalue()


def to_table(rows: Sequence[Mapping[str, Any]], fieldnames: Sequence[str]) -> str:
    cells = [[str(_cell(r.get(k))) for k in fieldnames] for r in rows]
    widths = [
        max([len(name)] + [len(c[i]) for c in cells])
        for i, name in enumerate(fieldnames)
    ]
    lines = ["  ".join(n.ljust(w) for n, w in zip(fieldnames, widths, strict=True))]
    lines.append("  ".join("-" * w for w in widths))
    for c in cells:
        lines.append("  ".join(v.ljust(w) for v, w in zip(c, widths, strict=True)))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------- row makers

COMPLIANCE_FIELDS = (
    "workload_id",
    "policy_key",
    "rpo_minutes",
    "status",
    "compliance_ratio",
    "violation_count",
    "violation_seconds",
    "longest_gap_minutes",
    "recovery_points",
)
DAILY_COMPLIANCE_FIELDS = ("workload_id", "day", "violation_seconds")
TREND_FIELDS = (
    "workload_id",
    "day",
    "success_rate",
    "max_gap_minutes",
    "rolling_success_rate",
    "rolling_max_gap_minutes",
    "missing",
)
DRIFT_FIELDS = (
    "workload_id",
    "kind",
    "severity",
    "message",
    "baseline",
    "recent",
    "change_pct",
)
FORECAST_FIELDS = ("metric", "value")
RUN_FIELDS = (
    "run_id",
    "workload_id",
    "policy_key",
    "started_at",
    "ended_at",
    "status",
    "source_bytes",
    "written_bytes",
)


def compliance_rows(report: ComplianceReport) -> list[Row]:
    return [{k: getattr(o, k) for k in COMPLIANCE_FIELDS} for o in report.objects]


def daily_compliance_rows(report: ComplianceReport) -> list[Row]:
    return [
        {"workload_id": o.workload_id, "day": day, "violation_seconds": secs}
        for o in report.objects
        for day, secs in sorted(o.daily_violation_seconds.items())
    ]


def trend_rows(trends: Mapping[str, Sequence[TrendPoint]]) -> list[Row]:
    return [
        {"workload_id": oid, **{k: getattr(p, k) for k in TREND_FIELDS[1:]}}
        for oid in sorted(trends)
        for p in trends[oid]
    ]


def drift_rows(alerts: Sequence[DriftAlert]) -> list[Row]:
    return [{k: getattr(a, k) for k in DRIFT_FIELDS} for a in alerts]


def forecast_rows(fc: CapacityForecast) -> list[Row]:
    rows: list[Row] = [
        {"metric": "n_points", "value": fc.n_points},
        {"metric": "slope_bytes_per_day", "value": fc.slope_bytes_per_day},
        {"metric": "intercept_bytes", "value": fc.intercept_bytes},
        {"metric": "r_squared", "value": fc.r_squared},
        {"metric": "last_day", "value": fc.last_day},
        {"metric": "last_bytes", "value": fc.last_bytes},
        {"metric": "days_until_full", "value": fc.days_until_full},
        {"metric": "projected_full_date", "value": fc.projected_full_date},
    ]
    rows.extend(
        {"metric": f"projected_bytes_{p.days_ahead}d", "value": p.bytes}
        for p in fc.projections
    )
    return rows


def run_rows(conn: sqlite3.Connection) -> list[Row]:
    cols = ", ".join(RUN_FIELDS)
    cur = conn.execute(f"SELECT {cols} FROM runs ORDER BY ended_at, run_id")
    return [dict(zip(RUN_FIELDS, r, strict=True)) for r in cur]

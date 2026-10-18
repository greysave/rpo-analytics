"""Command line interface."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from rpo_analytics import __version__, export
from rpo_analytics.analytics import (
    DriftAlert,
    DriftConfig,
    Window,
    compute_compliance,
    compute_daily_stats,
    cumulative_written_bytes,
    detect_drift,
    forecast_capacity,
    resolve_window,
    rolling_trend,
)
from rpo_analytics.db import (
    LATEST_VERSION,
    SchemaError,
    connect,
    migrate,
    schema_version,
)
from rpo_analytics.fixtures import DEFAULT_START, generate_document
from rpo_analytics.ingest import ingest_document, load_document
from rpo_analytics.timeutil import get_zone, parse_timestamp

Rows = Sequence[export.Row]


def _emit(text: str, output: str | None) -> None:
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _render(
    fmt: str,
    rows: Rows,
    fields: Sequence[str],
    payload: object,
    output: str | None,
) -> None:
    if fmt == "json":
        text = export.to_json(payload)
    elif fmt == "csv":
        text = export.to_csv(rows, fields)
    else:
        text = export.to_table(rows, fields)
    _emit(text, output)


def _report_parent() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--days", type=int, default=30, help="window length (default 30)")
    p.add_argument(
        "--end", help="window end, ISO-8601 with offset (default: newest run)"
    )
    p.add_argument("--tz", default="UTC", help="IANA timezone for day buckets")
    p.add_argument("--format", choices=("table", "json", "csv"), default="table")
    p.add_argument("--output", help="write to file instead of stdout")
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rpo-analytics", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--db", default="rpo.db", help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)
    rep = _report_parent()

    sub.add_parser("init", help="create or migrate the database")

    gen = sub.add_parser("generate", help="write a seeded synthetic document")
    gen.add_argument("--seed", type=int, default=7)
    gen.add_argument("--days", type=int, default=45)
    gen.add_argument("--objects", type=int, default=9)
    gen.add_argument("--start", default=DEFAULT_START.strftime("%Y-%m-%dT%H:%M:%SZ"))
    gen.add_argument("--output", required=True)

    ing = sub.add_parser("ingest", help="idempotently ingest a document")
    ing.add_argument("input")

    comp = sub.add_parser("compliance", parents=[rep], help="RPO compliance")
    comp.add_argument("--daily", action="store_true", help="per-day violation rows")
    comp.add_argument("--fail-on-violation", action="store_true")

    drift = sub.add_parser("drift", parents=[rep], help="drift alerts")
    drift.add_argument("--baseline-days", type=int, default=14)
    drift.add_argument("--recent-days", type=int, default=7)
    drift.add_argument("--gap-increase-pct", type=float, default=25.0)
    drift.add_argument("--fail-on-alert", action="store_true")

    trend = sub.add_parser("trend", parents=[rep], help="rolling daily trends")
    trend.add_argument("--rolling", type=int, default=7)
    trend.add_argument("--object", dest="workload_id")

    fc = sub.add_parser("forecast", parents=[rep], help="linear capacity forecast")
    fc.add_argument("--capacity-limit-bytes", type=int)

    runs = sub.add_parser("export-runs", help="dump raw runs")
    runs.add_argument("--format", choices=("json", "csv"), default="csv")
    runs.add_argument("--output")
    return parser


def _window(conn: sqlite3.Connection, args: argparse.Namespace) -> Window:
    end = parse_timestamp(args.end) if args.end else None
    window = resolve_window(conn, get_zone(args.tz), args.days, end)
    if window is None:
        raise ValueError("database has no runs; ingest data first")
    return window


def _cmd_init(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    applied = migrate(conn)
    sys.stdout.write(f"schema v{schema_version(conn)} (applied: {applied or 'none'})\n")
    return 0


def _cmd_generate(_: sqlite3.Connection | None, args: argparse.Namespace) -> int:
    doc = generate_document(
        args.seed, args.days, args.objects, parse_timestamp(args.start)
    )
    _emit(export.to_json(doc), args.output)
    return 0


def _cmd_ingest(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    migrate(conn)
    result = ingest_document(conn, load_document(args.input))
    sys.stdout.write(export.to_json(result.as_dict()))
    return 1 if result.rejected or result.conflicts else 0


def _cmd_compliance(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    report = compute_compliance(conn, _window(conn, args))
    payload: dict[str, Any] = {
        "window": report.window,
        "overall_ratio": report.overall_ratio,
        "objects": report.objects,
    }
    if args.daily:
        daily = export.daily_compliance_rows(report)
        _render(
            args.format, daily, export.DAILY_COMPLIANCE_FIELDS, payload, args.output
        )
    else:
        rows = export.compliance_rows(report)
        _render(args.format, rows, export.COMPLIANCE_FIELDS, payload, args.output)
    violated = any(o.status == "violated" for o in report.objects)
    return 2 if args.fail_on_violation and violated else 0


def _cmd_drift(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    window = _window(conn, args)
    cfg = DriftConfig(
        baseline_days=args.baseline_days,
        recent_days=args.recent_days,
        gap_increase_pct=args.gap_increase_pct,
    )
    rpo = {
        o.workload_id: o.rpo_minutes for o in compute_compliance(conn, window).objects
    }
    alerts: list[DriftAlert] = []
    for oid, stats in compute_daily_stats(conn, window).items():
        alerts.extend(detect_drift(oid, stats, rpo.get(oid, 1), cfg))
    _render(
        args.format, export.drift_rows(alerts), export.DRIFT_FIELDS, alerts, args.output
    )
    actionable = any(a.severity != "info" for a in alerts)
    return 2 if args.fail_on_alert and actionable else 0


def _cmd_trend(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    stats = compute_daily_stats(conn, _window(conn, args))
    if args.workload_id:
        if args.workload_id not in stats:
            raise ValueError(f"unknown object: {args.workload_id}")
        stats = {args.workload_id: stats[args.workload_id]}
    trends = {oid: rolling_trend(s, args.rolling) for oid, s in stats.items()}
    _render(
        args.format, export.trend_rows(trends), export.TREND_FIELDS, trends, args.output
    )
    return 0


def _cmd_forecast(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    series = cumulative_written_bytes(conn, _window(conn, args))
    fc = forecast_capacity(series, args.capacity_limit_bytes)
    _render(
        args.format, export.forecast_rows(fc), export.FORECAST_FIELDS, fc, args.output
    )
    return 0


def _cmd_export_runs(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    rows = export.run_rows(conn)
    text = (
        export.to_json(rows)
        if args.format == "json"
        else export.to_csv(rows, export.RUN_FIELDS)
    )
    _emit(text, args.output)
    return 0


# Only these may create or modify the database; every other command opens it read-only.
WRITE_COMMANDS = frozenset({"init", "ingest"})

COMMANDS: dict[str, Callable[[Any, argparse.Namespace], int]] = {
    "init": _cmd_init,
    "generate": _cmd_generate,
    "ingest": _cmd_ingest,
    "compliance": _cmd_compliance,
    "drift": _cmd_drift,
    "trend": _cmd_trend,
    "forecast": _cmd_forecast,
    "export-runs": _cmd_export_runs,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = COMMANDS[args.command]
    try:
        if args.command == "generate":
            return handler(None, args)
        writes = args.command in WRITE_COMMANDS
        conn = connect(args.db, read_only=not writes)
        try:
            if not writes:
                have = schema_version(conn)
                if have != LATEST_VERSION:
                    raise SchemaError(
                        f"database is schema v{have}, expected v{LATEST_VERSION}; "
                        "run 'rpo-analytics init' first"
                    )
            return handler(conn, args)
        finally:
            conn.close()
    except (ValueError, SchemaError, sqlite3.Error, OSError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1

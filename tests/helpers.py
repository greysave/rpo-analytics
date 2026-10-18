"""Shared builders for tests."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from rpo_analytics.db import connect, migrate
from rpo_analytics.ingest import FORMAT, ingest_document
from rpo_analytics.timeutil import format_timestamp

OPEN: list[sqlite3.Connection] = []
T0 = datetime(2026, 3, 1, tzinfo=UTC)


def new_conn() -> sqlite3.Connection:
    """Unmigrated in-memory connection, closed by the autouse fixture."""
    conn = connect(":memory:")
    OPEN.append(conn)
    return conn


def make_db() -> sqlite3.Connection:
    conn = new_conn()
    migrate(conn)
    return conn


def run(
    run_id: str,
    ended: datetime,
    *,
    workload_id: str = "vm-001",
    policy: str = "p60",
    status: str = "success",
    stored: int = 0,
    duration_s: int = 60,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "workload_id": workload_id,
        "policy_key": policy,
        "started_at": format_timestamp(ended - timedelta(seconds=duration_s)),
        "ended_at": format_timestamp(ended),
        "status": status,
        "source_bytes": 1000,
        "written_bytes": stored,
    }


def load(
    conn: sqlite3.Connection,
    runs: list[dict[str, Any]],
    rpo: int = 60,
) -> None:
    doc = {
        "format": FORMAT,
        "policies": [{"policy_key": "p60", "rpo_minutes": rpo}],
        "runs": runs,
    }
    ingest_document(conn, doc)

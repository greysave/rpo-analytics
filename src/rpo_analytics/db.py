"""SQLite connection handling and forward-only schema migrations.

The schema version lives in ``PRAGMA user_version``. Each migration runs in its
own transaction together with the version bump, so a failure leaves the
database at the previous version.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    description: str
    statements: tuple[str, ...]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        1,
        "policies and runs",
        (
            """CREATE TABLE policies (
                policy_key TEXT PRIMARY KEY,
                rpo_minutes INTEGER NOT NULL CHECK (rpo_minutes > 0)
            )""",
            """CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                workload_id TEXT NOT NULL,
                policy_key TEXT NOT NULL REFERENCES policies (policy_key),
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN ('success', 'warning', 'failed')),
                source_bytes INTEGER NOT NULL CHECK (source_bytes >= 0),
                content_hash TEXT NOT NULL
            )""",
        ),
    ),
    Migration(
        2,
        "add written_bytes for capacity forecasting",
        ("ALTER TABLE runs ADD COLUMN written_bytes INTEGER NOT NULL DEFAULT 0",),
    ),
    Migration(
        3,
        "index runs by object and end time",
        ("CREATE INDEX idx_runs_object_ended ON runs (workload_id, ended_at)",),
    ),
)

LATEST_VERSION = MIGRATIONS[-1].version


class SchemaError(RuntimeError):
    """The database schema cannot be used by this version of the tool."""


def connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a connection in autocommit mode; transactions are explicit.

    With ``read_only=True`` the database must already exist: it is opened with SQLite's
    ``mode=ro`` (so a mistyped path can never create an empty file) and with
    ``query_only`` so no statement can write.
    """
    if read_only:
        if str(path) == ":memory:":
            raise ValueError("an in-memory database cannot be opened read-only")
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(
                f"database not found: {path} (run 'rpo-analytics init' to create it)"
            )
        conn = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro", uri=True, isolation_level=None
        )
        conn.execute("PRAGMA query_only = ON")
    else:
        conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0])


def migrate(
    conn: sqlite3.Connection,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> list[int]:
    """Apply pending migrations; return the versions applied."""
    current = schema_version(conn)
    newest = migrations[-1].version
    if current > newest:
        raise SchemaError(
            f"database schema v{current} is newer than supported v{newest}"
        )
    applied: list[int] = []
    for mig in migrations:
        if mig.version <= current:
            continue
        conn.execute("BEGIN")
        try:
            for stmt in mig.statements:
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {int(mig.version)}")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        applied.append(mig.version)
    return applied

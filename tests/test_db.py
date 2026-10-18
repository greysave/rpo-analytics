import sqlite3

import pytest

from rpo_analytics.db import (
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    SchemaError,
    migrate,
    schema_version,
)
from tests.helpers import new_conn


def test_fresh_database_migrates_to_latest() -> None:
    conn = new_conn()
    assert schema_version(conn) == 0
    assert migrate(conn) == [m.version for m in MIGRATIONS]
    assert schema_version(conn) == LATEST_VERSION


def test_migrate_is_idempotent() -> None:
    conn = new_conn()
    migrate(conn)
    assert migrate(conn) == []
    assert schema_version(conn) == LATEST_VERSION


def test_upgrade_from_v1_preserves_rows_and_defaults_new_column() -> None:
    conn = new_conn()
    migrate(conn, MIGRATIONS[:1])
    assert schema_version(conn) == 1
    conn.execute("INSERT INTO policies VALUES ('gold', 60)")
    conn.execute(
        "INSERT INTO runs VALUES ('r1', 'vm-001', 'gold', "
        "'2026-03-01T00:00:00Z', '2026-03-01T00:01:00Z', 'success', 10, 'h')"
    )
    assert migrate(conn) == [2, 3]
    row = conn.execute("SELECT run_id, written_bytes FROM runs").fetchone()
    assert (row["run_id"], row["written_bytes"]) == ("r1", 0)
    index = conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'idx_runs_object_ended'"
    ).fetchone()
    assert index is not None


def test_newer_database_is_refused() -> None:
    conn = new_conn()
    conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    with pytest.raises(SchemaError):
        migrate(conn)


def test_failed_migration_rolls_back_including_version() -> None:
    conn = new_conn()
    migrate(conn, MIGRATIONS[:1])
    broken = (
        *MIGRATIONS[:1],
        Migration(
            2,
            "broken",
            ("CREATE TABLE half_done (x INTEGER)", "ALTER TABLE nope ADD COLUMN y"),
        ),
    )
    with pytest.raises(sqlite3.Error):
        migrate(conn, broken)
    assert schema_version(conn) == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "half_done" not in tables
    assert migrate(conn) == [2, 3]


def test_schema_enforces_status_check() -> None:
    conn = new_conn()
    migrate(conn)
    conn.execute("INSERT INTO policies VALUES ('gold', 60)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO runs (run_id, workload_id, policy_key, started_at, ended_at,"
            " status, source_bytes, content_hash) VALUES "
            "('r', 'o', 'gold', 'a', 'b', 'bogus', 1, 'h')"
        )

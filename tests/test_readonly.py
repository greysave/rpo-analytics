import sqlite3
from pathlib import Path

import pytest

from rpo_analytics.cli import COMMANDS, WRITE_COMMANDS, main
from rpo_analytics.db import connect

READ_COMMANDS: list[list[str]] = [
    ["compliance"],
    ["compliance", "--daily"],
    ["drift"],
    ["trend"],
    ["forecast"],
    ["export-runs"],
]


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    doc, db = tmp_path / "doc.json", tmp_path / "rpo.db"
    assert main(["generate", "--seed", "3", "--days", "20", "--output", str(doc)]) == 0
    assert main(["--db", str(db), "init"]) == 0
    assert main(["--db", str(db), "ingest", str(doc)]) == 0
    return db


@pytest.mark.parametrize("command", READ_COMMANDS)
def test_mistyped_db_path_is_an_error_and_creates_nothing(
    command: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "rpo.dbb"
    assert main(["--db", str(missing), *command]) == 1
    assert "database not found" in capsys.readouterr().err
    assert not missing.exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("command", READ_COMMANDS)
def test_report_commands_leave_the_database_byte_identical(
    command: list[str], seeded_db: Path
) -> None:
    before = seeded_db.read_bytes()
    assert main(["--db", str(seeded_db), *command]) in (0, 2)
    assert seeded_db.read_bytes() == before
    assert {p.name for p in seeded_db.parent.iterdir()} == {"doc.json", "rpo.db"}


def test_read_only_connection_rejects_writes(seeded_db: Path) -> None:
    conn = connect(seeded_db, read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM runs")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE t (x)")
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] > 0
    finally:
        conn.close()


def test_read_only_rejects_missing_directory_and_memory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        connect(tmp_path / "nope" / "rpo.db", read_only=True)
    with pytest.raises(ValueError, match="in-memory"):
        connect(":memory:", read_only=True)


def test_read_only_handles_awkward_paths(tmp_path: Path) -> None:
    odd = tmp_path / "my db #1?x%20y.db"
    rw = connect(odd)
    rw.execute("CREATE TABLE t (x)")
    rw.close()
    ro = connect(odd, read_only=True)
    try:
        assert ro.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    finally:
        ro.close()


def test_directory_path_is_an_error(tmp_path: Path) -> None:
    assert main(["--db", str(tmp_path), "compliance"]) == 1


def test_init_and_ingest_remain_the_only_writers() -> None:
    assert frozenset({"init", "ingest"}) == WRITE_COMMANDS
    assert set(COMMANDS) >= WRITE_COMMANDS


def test_ingest_still_creates_a_new_database(tmp_path: Path) -> None:
    doc, db = tmp_path / "doc.json", tmp_path / "new.db"
    assert main(["generate", "--seed", "1", "--days", "5", "--output", str(doc)]) == 0
    assert main(["--db", str(db), "ingest", str(doc)]) == 0
    assert db.exists()

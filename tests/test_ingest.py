from datetime import timedelta
from typing import Any

import pytest

from rpo_analytics.ingest import FORMAT, ingest_document
from tests.helpers import T0, load, make_db, run


def _count(conn: Any) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])


def test_duplicate_ingest_is_idempotent() -> None:
    conn = make_db()
    runs = [run("r1", T0 + timedelta(hours=1)), run("r2", T0 + timedelta(hours=2))]
    doc = {
        "format": FORMAT,
        "policies": [{"policy_key": "p60", "rpo_minutes": 60}],
        "runs": runs,
    }
    first = ingest_document(conn, doc)
    second = ingest_document(conn, doc)
    assert (first.inserted, first.duplicates) == (2, 0)
    assert (second.inserted, second.duplicates) == (0, 2)
    assert _count(conn) == 2


def test_duplicate_within_one_document() -> None:
    conn = make_db()
    one = run("r1", T0 + timedelta(hours=1))
    load(conn, [one, dict(one)])
    assert _count(conn) == 1


def test_same_instant_different_offset_is_still_a_duplicate() -> None:
    conn = make_db()
    a = run("r1", T0 + timedelta(hours=1))
    b = dict(
        a, started_at="2026-03-01T10:00:00+09:00", ended_at="2026-03-01T10:01:00+09:00"
    )
    # a: 00:59:00Z..01:00:00Z ; b is 01:00:00Z..01:01:00Z -> different content
    assert (
        ingest_document(
            conn,
            {
                "format": FORMAT,
                "policies": [{"policy_key": "p60", "rpo_minutes": 60}],
                "runs": [a],
            },
        ).inserted
        == 1
    )
    same = dict(
        a,
        started_at="2026-03-01T09:59:00+09:00",
        ended_at="2026-03-01T10:00:00+09:00",
    )
    res = ingest_document(conn, {"format": FORMAT, "runs": [same]})
    assert res.duplicates == 1
    res = ingest_document(conn, {"format": FORMAT, "runs": [b]})
    assert res.conflicts == 1


def test_conflicting_reuse_of_run_id_keeps_original() -> None:
    conn = make_db()
    load(conn, [run("r1", T0 + timedelta(hours=1), status="success")])
    doc = {
        "format": FORMAT,
        "runs": [run("r1", T0 + timedelta(hours=1), status="failed")],
    }
    result = ingest_document(conn, doc)
    assert result.conflicts == 1
    status = conn.execute("SELECT status FROM runs WHERE run_id = 'r1'").fetchone()[0]
    assert status == "success"


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"started_at": "2026-03-01T00:00:00"}, "offset"),
        ({"status": "exploded"}, "status"),
        ({"workload_id": "../etc"}, "workload_id"),
        ({"policy_key": "ghost"}, "unknown policy"),
        ({"source_bytes": -1}, "source_bytes"),
        ({"written_bytes": True}, "written_bytes"),
        ({"ended_at": "2026-02-28T00:00:00Z"}, "precedes"),
    ],
)
def test_invalid_runs_are_rejected_not_fatal(
    patch: dict[str, Any], reason: str
) -> None:
    conn = make_db()
    good = run("good", T0 + timedelta(hours=1))
    bad = {**run("bad", T0 + timedelta(hours=2)), **patch}
    load(conn, [bad, good])
    assert _count(conn) == 1
    result = ingest_document(conn, {"format": FORMAT, "runs": [bad]})
    assert len(result.rejected) == 1
    assert reason in result.rejected[0][1]


def test_non_object_run_is_rejected() -> None:
    conn = make_db()
    result = ingest_document(conn, {"format": FORMAT, "runs": ["nope"]})
    assert result.rejected == [(0, "run must be an object")]


def test_policy_update_and_validation() -> None:
    conn = make_db()
    load(conn, [], rpo=60)
    load(conn, [], rpo=120)
    assert conn.execute("SELECT rpo_minutes FROM policies").fetchone()[0] == 120
    with pytest.raises(ValueError):
        ingest_document(
            conn,
            {"format": FORMAT, "policies": [{"policy_key": "x", "rpo_minutes": 0}]},
        )
    with pytest.raises(ValueError):
        ingest_document(conn, {"format": FORMAT, "policies": ["x"]})


def test_bad_policy_rolls_back_whole_document() -> None:
    conn = make_db()
    doc = {
        "format": FORMAT,
        "policies": [
            {"policy_key": "ok", "rpo_minutes": 5},
            {"policy_key": "bad", "rpo_minutes": -1},
        ],
    }
    with pytest.raises(ValueError):
        ingest_document(conn, doc)
    assert conn.execute("SELECT COUNT(*) FROM policies").fetchone()[0] == 0


def test_wrong_format_rejected() -> None:
    conn = make_db()
    with pytest.raises(ValueError):
        ingest_document(conn, {"format": "other/9"})
    with pytest.raises(ValueError):
        ingest_document(conn, {"format": FORMAT, "runs": {}})

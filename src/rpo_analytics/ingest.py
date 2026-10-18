"""Idempotent ingestion of the synthetic ``rpo-analytics/1`` document format.

A document is a JSON object::

    {"format": "rpo-analytics/1",
     "policies": [{"policy_key": "gold", "rpo_minutes": 60}],
     "runs": [{"run_id": "r1", "workload_id": "vm-001", "policy_key": "gold",
               "started_at": "2026-01-01T00:00:00Z",
               "ended_at": "2026-01-01T00:05:00Z", "status": "success",
               "source_bytes": 1000, "written_bytes": 10}]}

Re-ingesting the same run is a no-op. Re-using a ``run_id`` with different
content is reported as a conflict and the stored row is left untouched.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rpo_analytics.timeutil import format_timestamp, parse_timestamp

FORMAT = "rpo-analytics/1"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
STATUSES = frozenset({"success", "warning", "failed"})


@dataclass(slots=True)
class IngestResult:
    inserted: int = 0
    duplicates: int = 0
    conflicts: int = 0
    policies_upserted: int = 0
    rejected: list[tuple[int, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "duplicates": self.duplicates,
            "conflicts": self.conflicts,
            "policies_upserted": self.policies_upserted,
            "rejected": [{"index": i, "reason": r} for i, r in self.rejected],
        }


def load_document(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        raise ValueError("document must be a JSON object")
    return doc


def _ident(value: object, name: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _parse_run(raw: object) -> tuple[Any, ...]:
    """Validate one run; return the row tuple (without hash)."""
    if not isinstance(raw, dict):
        raise ValueError("run must be an object")
    run_id = _ident(raw.get("run_id"), "run_id")
    workload_id = _ident(raw.get("workload_id"), "workload_id")
    policy_key = _ident(raw.get("policy_key"), "policy_key")
    started = parse_timestamp(raw.get("started_at"))
    ended = parse_timestamp(raw.get("ended_at"))
    if ended < started:
        raise ValueError("ended_at precedes started_at")
    status = raw.get("status")
    if status not in STATUSES:
        raise ValueError("invalid status")
    logical = _count(raw.get("source_bytes"), "source_bytes")
    stored = _count(raw.get("written_bytes", 0), "written_bytes")
    return (
        run_id,
        workload_id,
        policy_key,
        format_timestamp(started),
        format_timestamp(ended),
        status,
        logical,
        stored,
    )


def _hash(row: tuple[Any, ...]) -> str:
    payload = json.dumps(row[1:], separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def ingest_document(conn: sqlite3.Connection, doc: dict[str, Any]) -> IngestResult:
    """Ingest a document in a single transaction."""
    if doc.get("format") != FORMAT:
        raise ValueError(f"unsupported document format: {doc.get('format')!r}")
    result = IngestResult()
    policies = doc.get("policies", [])
    runs = doc.get("runs", [])
    if not isinstance(policies, list) or not isinstance(runs, list):
        raise ValueError("'policies' and 'runs' must be lists")

    conn.execute("BEGIN")
    try:
        for pol in policies:
            if not isinstance(pol, dict):
                raise ValueError("policy must be an object")
            policy_key = _ident(pol.get("policy_key"), "policy_key")
            rpo = pol.get("rpo_minutes")
            if isinstance(rpo, bool) or not isinstance(rpo, int) or rpo <= 0:
                raise ValueError("rpo_minutes must be a positive integer")
            conn.execute(
                "INSERT INTO policies (policy_key, rpo_minutes) VALUES (?, ?) "
                "ON CONFLICT (policy_key) DO UPDATE SET "
                "rpo_minutes = excluded.rpo_minutes",
                (policy_key, rpo),
            )
            result.policies_upserted += 1

        known = {r[0] for r in conn.execute("SELECT policy_key FROM policies")}
        for index, raw in enumerate(runs):
            try:
                row = _parse_run(raw)
            except ValueError as exc:
                result.rejected.append((index, str(exc)))
                continue
            if row[2] not in known:
                result.rejected.append((index, "unknown policy_key"))
                continue
            digest = _hash(row)
            existing = conn.execute(
                "SELECT content_hash FROM runs WHERE run_id = ?", (row[0],)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO runs (run_id, workload_id, policy_key, started_at,"
                    " ended_at, status, source_bytes, written_bytes, content_hash)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*row, digest),
                )
                result.inserted += 1
            elif existing[0] == digest:
                result.duplicates += 1
            else:
                result.conflicts += 1
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return result

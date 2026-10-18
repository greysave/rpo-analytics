"""Seeded synthetic data generator. No real systems, names or data involved."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import Any

from rpo_analytics.ingest import FORMAT
from rpo_analytics.timeutil import format_timestamp

POLICIES = (("gold", 60), ("silver", 240), ("bronze", 1440))
DEFAULT_START = datetime(2026, 1, 1, tzinfo=UTC)


def generate_document(
    seed: int = 7,
    days: int = 45,
    objects: int = 9,
    start: datetime = DEFAULT_START,
) -> dict[str, Any]:
    """Build a reproducible document.

    Built-in scenarios: object 2 (``silver``) slows down after 70% of the
    period (drift) and object 3 (``bronze``) has an outage in the final 20%
    of the period (missing data).
    """
    if days < 1 or objects < 1:
        raise ValueError("days and objects must be positive")
    rng = random.Random(seed)
    end = start + timedelta(days=days)
    runs: list[dict[str, Any]] = []
    for idx in range(1, objects + 1):
        policy, rpo = POLICIES[(idx - 1) % len(POLICIES)]
        workload_id = f"vm-{idx:03d}"
        base_logical = rng.randint(50, 500) * 10**9
        step = timedelta(minutes=rpo * 0.75)
        cursor = start + timedelta(minutes=rng.randint(0, rpo // 2))
        n = 0
        while cursor < end:
            progress = (cursor - start) / (end - start)
            interval = step
            if idx == 2 and progress > 0.7:
                interval = step * 1.8
            outage = idx == 3 and 0.8 < progress < 1.0
            if not outage:
                n += 1
                duration = timedelta(seconds=rng.randint(30, 900))
                roll = rng.random()
                status = (
                    "failed" if roll < 0.03 else "warning" if roll < 0.08 else "success"
                )
                growth = 1 + 0.02 * (cursor - start).days
                stored = (
                    0
                    if status == "failed"
                    else int(base_logical * 0.002 * growth * rng.uniform(0.8, 1.2))
                )
                runs.append(
                    {
                        "run_id": f"run-{workload_id}-{n:06d}",
                        "workload_id": workload_id,
                        "policy_key": policy,
                        "started_at": format_timestamp(cursor),
                        "ended_at": format_timestamp(cursor + duration),
                        "status": status,
                        "source_bytes": base_logical,
                        "written_bytes": stored,
                    }
                )
            cursor += interval + timedelta(seconds=rng.randint(-120, 120))
    return {
        "format": FORMAT,
        "policies": [{"policy_key": p, "rpo_minutes": r} for p, r in POLICIES],
        "runs": runs,
    }

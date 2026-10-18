import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest

from rpo_analytics.cli import main
from rpo_analytics.export import to_csv, to_json, to_table
from rpo_analytics.fixtures import generate_document


@pytest.fixture(scope="module")
def seeded(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("cli")
    doc = base / "doc.json"
    db = base / "rpo.db"
    assert main(["generate", "--seed", "3", "--days", "45", "--output", str(doc)]) == 0
    assert main(["--db", str(db), "init"]) == 0
    assert main(["--db", str(db), "ingest", str(doc)]) == 0
    return base


def test_generator_is_deterministic_and_seed_sensitive() -> None:
    assert generate_document(5, 3, 3) == generate_document(5, 3, 3)
    assert generate_document(5, 3, 3) != generate_document(6, 3, 3)
    with pytest.raises(ValueError):
        generate_document(days=0)


def test_reingest_via_cli_reports_only_duplicates(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert (
        main(["--db", str(seeded / "rpo.db"), "ingest", str(seeded / "doc.json")]) == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["inserted"] == 0 and out["duplicates"] > 0 and out["conflicts"] == 0


def test_compliance_json_and_csv(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(seeded / "rpo.db")
    capsys.readouterr()
    assert (
        main(["--db", db, "compliance", "--format", "json", "--tz", "Asia/Tokyo"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["objects"]) == 9
    assert 0.0 <= payload["overall_ratio"] <= 1.0
    assert main(["--db", db, "compliance", "--format", "csv", "--daily"]) == 0
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert rows and set(rows[0]) == {"workload_id", "day", "violation_seconds"}


def test_seeded_scenarios_trigger_drift_and_missing_data(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(seeded / "rpo.db")
    capsys.readouterr()
    assert main(["--db", db, "drift", "--format", "json", "--fail-on-alert"]) == 2
    alerts = json.loads(capsys.readouterr().out)
    kinds = {(a["workload_id"], a["kind"]) for a in alerts}
    assert ("vm-002", "rpo_drift") in kinds
    assert ("vm-003", "no_recent_data") in kinds


def test_trend_forecast_and_export(
    seeded: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    db = str(seeded / "rpo.db")
    capsys.readouterr()
    assert main(["--db", db, "trend", "--object", "vm-001", "--format", "csv"]) == 0
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert len(rows) >= 30 and {r["workload_id"] for r in rows} == {"vm-001"}

    assert (
        main(
            [
                "--db",
                db,
                "forecast",
                "--capacity-limit-bytes",
                "10000000000000",
                "--format",
                "json",
            ]
        )
        == 0
    )
    fc = json.loads(capsys.readouterr().out)
    assert fc["slope_bytes_per_day"] > 0 and fc["projected_full_date"]

    out = tmp_path / "runs.csv"
    assert main(["--db", db, "export-runs", "--output", str(out)]) == 0
    header = out.read_text().splitlines()[0]
    assert header.startswith("run_id,workload_id,policy_key")
    assert main(["--db", db, "export-runs", "--format", "json"]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_table_output_and_fail_on_violation(
    seeded: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(seeded / "rpo.db")
    capsys.readouterr()
    code = main(["--db", db, "compliance", "--fail-on-violation"])
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("workload_id")
    assert code in (0, 2)


def test_errors_are_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "x.db")
    assert main(["--db", db, "compliance"]) == 1  # not initialised
    assert "run 'rpo-analytics init'" in capsys.readouterr().err
    assert main(["--db", db, "init"]) == 0
    assert main(["--db", db, "compliance"]) == 1  # no runs
    assert "no runs" in capsys.readouterr().err
    assert main(["--db", db, "compliance", "--tz", "Nope/Zone"]) == 1
    missing = str(tmp_path / "missing.json")
    assert main(["--db", db, "ingest", missing]) == 1


def test_ingest_with_rejections_exits_nonzero(tmp_path: Path) -> None:
    doc = tmp_path / "d.json"
    doc.write_text(json.dumps({"format": "rpo-analytics/1", "runs": [{"run_id": "x"}]}))
    assert main(["--db", str(tmp_path / "y.db"), "ingest", str(doc)]) == 1


def test_export_helpers() -> None:
    rows: list[dict[str, Any]] = [
        {"a": 1, "b": None, "c": 0.5},
        {"a": "x,y", "b": 2.0, "c": 'q"r'},
    ]
    text = to_csv(rows, ["a", "b", "c"])
    assert text.splitlines()[0] == "a,b,c"
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert parsed[0] == {"a": "1", "b": "", "c": "0.5"}
    assert parsed[1] == {"a": "x,y", "b": "2", "c": 'q"r'}
    table = to_table(rows, ["a", "c"])
    assert table.splitlines()[1].startswith("-")
    assert json.loads(to_json({"k": rows})) == {"k": rows}
    with pytest.raises(TypeError):
        to_json({"x": object()})

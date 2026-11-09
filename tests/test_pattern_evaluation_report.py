"""Offline reports must reproduce and atomically become discoverable."""

import json
import sqlite3
from pathlib import Path

from tools.pattern_evaluation_events import Envelope, Manifest
from tools.pattern_evaluation_report import evaluate, main, read_evidence
from tools.pattern_evaluation_store import EvaluationStore

FIXTURES = Path("tests/fixtures/pattern_evaluation")


def inputs():
    definition = Manifest.model_validate_json((FIXTURES / "manifest.json").read_text())
    evidence = tuple(
        Envelope.model_validate_json(line)
        for line in (FIXTURES / "evidence.jsonl").read_text().splitlines()
    )
    return definition, evidence


def test_deterministic_eight_group_report_and_no_future_decision_changes():
    definition, evidence = inputs()
    first, rows = evaluate(definition, evidence)
    again, again_rows = evaluate(definition, evidence)
    assert first == again and rows == again_rows
    assert isinstance(first["groups"], list) and len(first["groups"]) == 8
    assert first["label"] == "synthetic"
    assert all(row["decision"] == "insufficient" for row in rows["groups"])
    assert isinstance(first["comparisons"], list) and len(first["comparisons"]) == 6
    assert isinstance(first["claims"], list) and len(first["claims"]) == 14
    assert "token" not in json.dumps(first).lower()


def test_cli_registration_idempotence_exports_and_exit_codes(tmp_path):
    store = tmp_path / "results.sqlite3"
    args = [
        "--manifest",
        str(FIXTURES / "manifest.json"),
        "--evidence",
        str(FIXTURES / "evidence.jsonl"),
        "--output",
        str(tmp_path / "output"),
        "--store",
        str(store),
    ]
    assert main(args) == 0
    registered = EvaluationStore(store).latest()
    assert registered is not None
    assert main(args) == 0
    assert EvaluationStore(store).latest() == registered
    assert (tmp_path / "output" / "report.json").is_file()
    assert (tmp_path / "output" / "report.html").is_file()
    assert (tmp_path / "output" / "complete.json").is_file()
    assert main([*args[:-2], "--store", str(tmp_path / "missing" / "db")]) == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert (
        main(
            [
                "--manifest",
                str(bad),
                "--evidence",
                str(FIXTURES / "evidence.jsonl"),
                "--output",
                str(tmp_path / "bad-output"),
            ]
        )
        == 2
    )


def test_legacy_import_is_readonly_partial_and_never_reconstructs_quotes(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE quotes(id INTEGER); INSERT INTO quotes VALUES(1);"
    )
    connection.close()
    before = path.read_bytes()
    definition, _ = inputs()
    evidence, states = read_evidence(path, definition)
    report, records = evaluate(definition, evidence, states)
    assert path.read_bytes() == before
    assert records["attempts"] == []
    assert "PARTIAL LEGACY IMPORT" in str(report["limitations"])
    assert all(row["decision"] == "insufficient" for row in records["groups"])

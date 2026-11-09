"""Authenticated bounded read views never trigger research processing."""

import csv
import io
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue

from tools import scan_server
from tools.pattern_evaluation_report import main
from tools.pattern_evaluation_store import RetiredRevision


@pytest.fixture
def client(monkeypatch):
    async def noop():
        return None

    monkeypatch.setattr(scan_server, "_init", noop)
    monkeypatch.setattr(scan_server, "_shutdown", noop)
    monkeypatch.setattr(scan_server, "_WEB_SESSION_TOKEN", "evaluation-test")
    with TestClient(scan_server.app, client=("127.0.0.1", 50000)) as browser:
        browser.cookies.set("fcc_session", "evaluation-test")
        yield browser


def publish():
    report: dict[str, JsonValue] = {
        "report_id": "a" * 64,
        "status": "completed",
        "groups": [],
    }
    scan_server._PATTERN_EVALUATION.store.register(
        report,
        {
            "attempts": [
                {
                    "market": "GOLD",
                    "variant": "confirmed-90",
                    "phase": "development",
                    "sequence": i,
                    "outcome": None,
                    "reason": "<b>literal</b>",
                }
                for i in range(130)
            ]
        },
    )
    return report


def test_auth_unavailable_storage_failure_and_no_replay(client, monkeypatch):
    client.cookies.clear()
    assert client.get("/api/pattern/evaluation").status_code == 401
    client.cookies.set("fcc_session", "evaluation-test")
    result = client.get("/api/pattern/evaluation")
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    assert result.json()["report"] is None
    assert "diagnostics" in result.json()

    def no_replay(*_args):
        raise AssertionError("GET must never read/replay evidence")

    monkeypatch.setattr(scan_server._PATTERN_EVALUATION.store, "evidence", no_replay)
    assert client.get("/api/pattern/evaluation").status_code == 200

    def failed():
        raise sqlite3.OperationalError("private local path")

    monkeypatch.setattr(scan_server._PATTERN_EVALUATION.store, "latest", failed)
    response = client.get("/api/pattern/evaluation")
    assert response.status_code == 503
    assert "private local path" not in response.text


def test_report_pagination_filters_and_full_csv(client):
    report = publish()
    query = f"report_id={report['report_id']}&kind=attempts&market=GOLD&variant=confirmed-90&phase=development"
    response = client.get("/api/pattern/evaluation/records?" + query)
    assert response.status_code == 200
    data = response.json()
    assert len(data["rows"]) == 25
    assert data["has_more"] is True
    assert data["rows"][0][data["columns"].index("outcome")] is None
    assert data["rows"][0][data["columns"].index("sequence")] == 0
    second = client.get(
        "/api/pattern/evaluation/records?" + query + "&offset=25&limit=100"
    ).json()
    assert second["rows"][0][second["columns"].index("sequence")] == 25
    assert len(second["rows"]) == 100
    exported = client.get(
        "/api/pattern/evaluation/records.csv?" + query + "&offset=25&limit=1"
    )
    assert exported.status_code == 200
    assert len(list(csv.DictReader(io.StringIO(exported.text)))) == 130
    assert exported.headers["cache-control"] == "no-store"
    assert (
        client.get(
            "/api/pattern/evaluation/records?"
            + query.replace("market=GOLD", "market=OIL_CRUDE")
        ).json()["rows"]
        == []
    )


@pytest.mark.parametrize(
    "query",
    [
        "kind=attempts",
        "report_id=bad&kind=attempts",
        "report_id=" + "a" * 64 + "&diagnostic_revision=1&kind=attempts",
        "report_id=" + "a" * 64 + "&kind=secrets",
        "report_id=" + "a" * 64 + "&kind=attempts&limit=101",
        "report_id=" + "a" * 64 + "&kind=attempts&offset=-1",
        "report_id=" + "a" * 64 + "&kind=attempts&market=bad",
        "report_id=" + "a" * 64 + "&kind=attempts&variant=bad",
        "report_id=" + "a" * 64 + "&kind=attempts&phase=bad",
        "diagnostic_revision=1&kind=groups",
        "diagnostic_revision=-1&kind=attempts",
        "report_id=" + "a" * 64 + "&kind=attempts&path=C:/private",
    ],
)
def test_invalid_selections(client, query):
    response = client.get("/api/pattern/evaluation/records?" + query)
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"


def test_unknown_report_and_retired_diagnostic_revision(client, monkeypatch):
    assert (
        client.get(
            "/api/pattern/evaluation/records?report_id=" + "f" * 64 + "&kind=attempts"
        ).status_code
        == 404
    )

    def retired(*_args):
        raise RetiredRevision("refresh")

    monkeypatch.setattr(scan_server._PATTERN_EVALUATION, "diagnostic_records", retired)
    assert (
        client.get(
            "/api/pattern/evaluation/records?diagnostic_revision=1&kind=attempts"
        ).status_code
        == 409
    )


def test_diagnostics_available_without_comparison_report(client, monkeypatch):
    def diagnostic_records(revision, kind, filters, offset, limit):
        assert revision == 7 and kind == "stages" and offset == 0 and limit == 25
        return {
            "diagnostic_revision": 7,
            "columns": ["market", "count", "reason"],
            "rows": [["GOLD", 23, "No return"]],
            "offset": 0,
            "has_more": False,
            "limitations": [],
        }

    monkeypatch.setattr(
        scan_server._PATTERN_EVALUATION, "diagnostic_records", diagnostic_records
    )
    result = client.get(
        "/api/pattern/evaluation/records?diagnostic_revision=7&kind=stages"
    )
    assert result.status_code == 200
    assert result.json()["rows"][0][1] == 23


def test_diagnostic_csv_pins_once_before_revision_retirement(client, monkeypatch):
    calls = []

    def export(revision, kind, filters):
        calls.append((revision, kind, filters))
        return tuple(
            {"market": "GOLD", "count": i, "reason": "pinned"} for i in range(230)
        )

    def retired(*_args):
        raise RetiredRevision("Retired after pin")

    monkeypatch.setattr(scan_server._PATTERN_EVALUATION, "diagnostic_export", export)
    monkeypatch.setattr(scan_server._PATTERN_EVALUATION, "diagnostic_records", retired)
    response = client.get(
        "/api/pattern/evaluation/records.csv?diagnostic_revision=7&kind=stages&offset=200&limit=1"
    )
    assert response.status_code == 200
    assert len(list(csv.DictReader(io.StringIO(response.text)))) == 230
    assert calls == [(7, "stages", {})]


def test_cli_publication_is_discovered_by_authenticated_views(client, tmp_path):
    fixtures = Path("tests/fixtures/pattern_evaluation")
    assert (
        main(
            [
                "--manifest",
                str(fixtures / "manifest.json"),
                "--evidence",
                str(fixtures / "evidence.jsonl"),
                "--output",
                str(tmp_path / "exports"),
                "--store",
                str(scan_server._PATTERN_EVALUATION.store.path),
            ]
        )
        == 0
    )
    summary = client.get("/api/pattern/evaluation").json()
    assert summary["report_status"] == "completed"
    assert len(summary["report"]["groups"]) == 8
    report_id = summary["report"]["report_id"]
    records = client.get(
        f"/api/pattern/evaluation/records?report_id={report_id}&kind=groups"
    ).json()
    assert len(records["rows"]) == 8
    assert records["report_id"] == report_id

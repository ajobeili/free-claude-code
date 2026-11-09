"""Durable research outcomes, executable quote sides, and missing-data evidence."""

import asyncio
import csv
import io
from datetime import UTC, datetime, timedelta

import pytest

from tools.pattern_events import AttemptRecord, DecisionRecord, GapRecord, QuoteRecord
from tools.pattern_journal import JournalRecorder, JournalStore

NOW = datetime(2026, 11, 4, 10, tzinfo=UTC)


def attempt(sign=1):
    return AttemptRecord(
        "GOLD", NOW - timedelta(seconds=3), sign, 110, 90, 114 if sign == 1 else 86, 50
    )


def decision(outcome="qualified"):
    return DecisionRecord(
        "GOLD", attempt().started, NOW, outcome, "test reentry", 100, 101, 49
    )


def quote(seconds, sign=1):
    time = NOW + timedelta(seconds=seconds)
    return QuoteRecord(
        "GOLD",
        time,
        time + timedelta(seconds=13),
        100 - sign * seconds / 60,
        101 - sign * seconds / 60,
        "delayed_research",
    )


@pytest.mark.parametrize("sign", [1, -1])
def test_durable_one_three_five_minute_markouts_include_quote_spread(tmp_path, sign):
    store = JournalStore(tmp_path / "journal.sqlite3")
    store.start({"rule": "baseline"})
    store.write(
        [
            attempt(sign),
            quote(0, sign),
            decision(),
            *[quote(i, sign) for i in range(1, 301)],
        ]
    )
    rows = list(csv.DictReader(io.StringIO("".join(store.export("outcomes")))))
    assert [float(row["change_points"]) for row in rows] == [0, 2, 4]
    assert all(row["status"] == "complete" for row in rows)
    assert store.summary()["quotes"] == 301
    assert store.summary()["qualified"] == 1
    restarted = JournalStore(tmp_path / "journal.sqlite3")
    restarted.start({"rule": "baseline"})
    assert restarted.summary()["attempts"] == 1
    assert restarted.summary()["complete_outcomes"] == 3


def test_quote_gap_is_not_scored_as_a_winning_observation(tmp_path):
    store = JournalStore(tmp_path / "journal.sqlite3")
    store.start({})
    store.write([attempt(), quote(0), decision(), quote(60)])
    rows = list(csv.DictReader(io.StringIO("".join(store.export("outcomes")))))
    assert all(row["status"] == "incomplete" for row in rows)
    assert all(row["change_points"] == "" for row in rows)


def test_saved_record_lists_are_bounded_and_match_downloads(tmp_path):
    store = JournalStore(tmp_path / "journal.sqlite3")
    assert store.records("quotes")["rows"] == []
    store.start({"rule": "baseline"})
    store.write([attempt(), quote(0), decision(), *[quote(i) for i in range(1, 30)]])
    first = store.records("quotes")
    second = store.records("quotes", 25)
    assert len(first["rows"]) == 25
    assert first["has_more"] is True
    assert len(second["rows"]) == 5
    assert second["has_more"] is False
    assert first["rows"][-1][0] != second["rows"][0][0]
    for kind in ("quotes", "attempts", "outcomes", "runs", "ranges", "gaps"):
        page = store.records(kind, limit=100)
        exported = list(csv.reader(io.StringIO("".join(store.export(kind)))))
        assert page["columns"] == exported[0]
        assert len(page["rows"]) == len(exported) - 1
    assert store.records("quotes", 999)["rows"] == []
    for kind, offset, limit in (
        ("secrets", 0, 25),
        ("quotes", -1, 25),
        ("quotes", 0, 101),
    ):
        with pytest.raises(ValueError):
            store.records(kind, offset, limit)


def test_disconnect_and_restart_close_pending_evidence(tmp_path):
    store = JournalStore(tmp_path / "journal.sqlite3")
    store.start({})
    store.write([attempt(), quote(0), decision(), GapRecord(NOW, "feed_disconnected")])
    assert store.summary()["incomplete_outcomes"] == 3
    gaps = list(csv.DictReader(io.StringIO("".join(store.export("gaps")))))
    assert gaps[0]["reason"] == "feed_disconnected"
    store.write([AttemptRecord("OIL_CRUDE", NOW, 1, 90, 80, 91, 100)])
    restarted = JournalStore(tmp_path / "journal.sqlite3")
    restarted.start({})
    attempts = list(csv.DictReader(io.StringIO("".join(restarted.export("attempts")))))
    assert attempts[-1]["outcome"] == "interrupted"
    assert attempts[-1]["reason"] == "server_restart"


def test_rejected_context_is_recorded_without_fabricating_a_qualified_alert(tmp_path):
    store = JournalStore(tmp_path / "journal.sqlite3")
    store.start({})
    store.write(
        [
            attempt(),
            quote(0),
            decision("context_failed"),
            *[quote(i) for i in range(1, 61)],
        ]
    )
    assert store.summary()["reentries"] == 1
    assert store.summary()["qualified"] == 0
    assert store.summary()["complete_outcomes"] == 1


def test_export_rejects_unknown_tables(tmp_path):
    store = JournalStore(tmp_path / "journal.sqlite3")
    store.start({})
    assert list(store.export("secrets")) == []


def test_recorder_writes_in_background_and_drains_before_exit(tmp_path):
    async def run():
        recorder = JournalRecorder(tmp_path / "journal.sqlite3")
        await recorder.start({"rule": "unchanged"})
        worker = asyncio.create_task(recorder.run())
        recorder.enqueue(attempt())
        recorder.enqueue(quote(0))
        recorder.enqueue(decision())
        await asyncio.wait_for(recorder.drain(), 3)
        assert recorder.snapshot()["quotes"] == 1
        assert recorder.snapshot()["pending_outcomes"] == 3
        assert recorder.snapshot()["dropped"] == 0
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())


def test_storage_failure_is_visible_and_does_not_raise_in_quote_callback(tmp_path):
    async def run():
        blocked = tmp_path / "not-a-directory"
        blocked.write_text("blocked")
        recorder = JournalRecorder(blocked / "journal.sqlite3")
        await recorder.start({})
        recorder.enqueue(quote(0))
        assert recorder.snapshot()["running"] is False
        assert recorder.snapshot()["error"]
        assert recorder.snapshot()["dropped"] == 1

    asyncio.run(run())


def test_rule_versions_are_not_pooled_in_markout_averages(tmp_path):
    store = JournalStore(tmp_path / "journal.sqlite3")
    store.start({"rule": "first"})
    store.write([attempt(), quote(0), decision(), *[quote(i) for i in range(1, 61)]])
    assert store.summary()["markouts"]
    store.start({"rule": "second"})
    assert store.summary()["markouts"] == []
    assert store.summary()["complete_outcomes"] == 1


def test_recording_failure_excludes_run_from_markout_averages(tmp_path):
    store = JournalStore(tmp_path / "journal.sqlite3")
    store.start({})
    store.write([attempt(), quote(0), decision(), *[quote(i) for i in range(1, 61)]])
    assert store.summary()["markouts"]
    store.fail("recording_failed", 1)
    assert store.summary()["markouts"] == []


def test_queue_overflow_is_bounded_and_saved_as_incomplete_evidence(tmp_path):
    async def run():
        recorder = JournalRecorder(tmp_path / "journal.sqlite3")
        await recorder.start({})
        for _ in range(10001):
            recorder.enqueue(quote(0))
        assert recorder.snapshot()["pending_writes"] == 10000
        worker = asyncio.create_task(recorder.run())
        await asyncio.wait_for(recorder.drain(), 5)
        assert recorder.snapshot()["running"] is False
        runs = list(csv.DictReader(io.StringIO("".join(recorder.export("runs")))))
        assert "overflow" in runs[0]["recording_error"]
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())


def test_journal_endpoints_require_authentication_and_export_saved_evidence(
    tmp_path, monkeypatch
):
    from fastapi.testclient import TestClient

    from tools import scan_server

    async def noop():
        return None

    recorder = JournalRecorder(tmp_path / "journal.sqlite3")
    asyncio.run(recorder.start({"rule": "baseline"}))
    monkeypatch.setattr(scan_server, "_init", noop)
    monkeypatch.setattr(scan_server, "_shutdown", noop)
    monkeypatch.setattr(scan_server, "_WEB_SESSION_TOKEN", "journal-test")
    monkeypatch.setattr(scan_server, "_PATTERN_JOURNAL", recorder)
    with TestClient(scan_server.app, client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/pattern/journal").status_code == 401
        client.cookies.clear()
        assert client.get("/api/pattern/journal/runs.json").status_code == 401
        client.get("/pattern")
        assert client.get("/api/pattern/journal").json()["running"] is True
        exported = client.get("/api/pattern/journal/runs.csv")
        assert exported.status_code == 200
        assert 'filename="pattern-runs.csv"' in exported.headers["Content-Disposition"]
        assert "baseline" in exported.text
        records = client.get("/api/pattern/journal/runs.json").json()
        assert "baseline" in str(records["rows"])
        assert records["has_more"] is False
        assert client.get("/api/pattern/journal/secrets.json").status_code == 422
        assert (
            client.get("/api/pattern/journal/quotes.json?offset=-1").status_code == 422
        )
        assert client.get("/api/pattern/journal/secrets.csv").status_code == 422

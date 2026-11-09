"""Persistence failures cannot affect the operating scanner."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from pydantic import JsonValue, ValidationError

from tools.pattern_evaluation_events import ControlEvidence, Envelope, HistoryEvidence
from tools.pattern_evaluation_store import (
    EvaluationRecorder,
    EvaluationStore,
    RetiredRevision,
)


def event(sequence=1):
    return Envelope(
        run_id="test",
        sequence=sequence,
        kind="control",
        host_observed_utc=datetime.now(UTC),
        payload=ControlEvidence(reason="start", connected=True),
    )


def test_sequences_snapshots_and_registry_atomicity(tmp_path):
    store = EvaluationStore(tmp_path / "evaluation.db")
    store.append([event()])
    first = store.evidence()
    with pytest.raises(ValueError, match="sequence"):
        store.append([event()])
    with pytest.raises(ValueError, match="sequence"):
        store.append([event(3)])
    store.append([event(2)])
    assert len(first) == 1
    assert len(store.evidence()) == 2
    report: dict[str, JsonValue] = {
        "report_id": "a" * 64,
        "status": "completed",
        "groups": [],
    }
    store.register(report, {"groups": []})
    store.register(report, {"groups": []})
    assert store.latest() == report
    with pytest.raises(ValueError):
        store.register({"report_id": "b" * 64, "status": "pending"}, {})
    assert store.latest() == report


def test_bounded_queue_failure_and_drain(tmp_path):
    async def run():
        recorder = EvaluationRecorder(tmp_path / "evaluation.db", capacity=2)
        await recorder.start()
        recorder.control("one")
        recorder.control("two")
        recorder.control("overflow")
        assert recorder.snapshot()["dropped"] == 1
        assert recorder.snapshot()["error"]
        await recorder.drain()
        assert recorder.snapshot()["last_committed_sequence"] == 2
        await recorder.close()

    asyncio.run(run())


def test_writer_failure_is_latched(tmp_path, monkeypatch):
    async def run():
        recorder = EvaluationRecorder(tmp_path / "evaluation.db")
        await recorder.start()

        def fail(_events):
            raise OSError("private path must not leak")

        monkeypatch.setattr(recorder.store, "append", fail)
        recorder.control("test")
        await recorder.drain()
        assert (
            recorder.snapshot()["error"]
            == "Research recording failed; evidence is incomplete."
        )
        await recorder.close()

    asyncio.run(run())


def test_failed_batch_rolls_back_and_existing_snapshot_is_immutable(tmp_path):
    store = EvaluationStore(tmp_path / "atomic.db")
    with pytest.raises(ValueError):
        store.append([event(1), event(3)])
    assert store.evidence() == ()
    store.append([event(1)])
    cutoff = store.evidence()
    store.append([event(2)])
    assert tuple(item.sequence for item in cutoff) == (1,)
    assert tuple(item.sequence for item in store.evidence()) == (1, 2)


def test_history_identity_and_extra_private_fields_rejected():
    with pytest.raises(ValidationError):
        HistoryEvidence.model_validate(
            {
                "as_of": "2026-11-04T10:00:00Z",
                "bars": [],
                "complete": True,
                "request_sequence": 1,
            }
        )
    with pytest.raises(ValidationError):
        Envelope.model_validate({**event().model_dump(), "account_id": "secret"})


def test_diagnostics_commit_without_report_restart_and_retirement(tmp_path):
    async def run():
        path = tmp_path / "worker.db"
        recorder = EvaluationRecorder(path)
        await recorder.start()
        recorder.control("feed_connected", True)
        await recorder.drain()
        assert recorder.store.latest() is None
        assert recorder.diagnostics()["processed_through_sequence"] == 1
        first = recorder.diagnostics()["revision"]
        assert isinstance(first, int)
        recorder.control("next")
        await recorder.drain()
        recorder.control("next")
        await recorder.drain()
        with pytest.raises(RetiredRevision):
            recorder.diagnostic_records(first, "attempts", {}, 0, 25)
        await recorder.close()
        restarted = EvaluationRecorder(path)
        await restarted.start()
        restarted.control("restart", False)
        await restarted.drain()
        assert restarted.diagnostics()["run_id"] == restarted.snapshot()["run_id"]
        assert len(restarted.store.evidence()) == 4
        await restarted.close()

    asyncio.run(run())


def test_capture_and_consumer_failure_are_distinct(tmp_path, monkeypatch):
    async def run():
        recorder = EvaluationRecorder(tmp_path / "diagnostic-failure.db")

        def fail(_events):
            raise RuntimeError("private exception")

        monkeypatch.setattr(recorder, "_reduce_batch", fail)
        await recorder.start()
        recorder.control("connected", True)
        await recorder.drain()
        assert recorder.snapshot()["error"] == ""
        assert recorder.diagnostics()["status"] == "error"
        assert "private" not in str(recorder.diagnostics())
        assert recorder.snapshot()["last_committed_sequence"] == 1
        await recorder.close()

    asyncio.run(run())


def test_unavailable_storage_is_visible_without_raising(tmp_path):
    async def run():
        recorder = EvaluationRecorder(tmp_path / "missing" / "unavailable.db")
        await recorder.start()
        assert recorder.snapshot()["running"] is False
        assert recorder.snapshot()["error"]
        await recorder.close()

    asyncio.run(run())


def test_default_queue_capacity_and_writer_batch_ceiling(tmp_path, monkeypatch):
    """Exercise the actual production capacity, rather than only a tiny proxy."""

    async def run():
        recorder = EvaluationRecorder(tmp_path / "full-queue.db")
        sizes = []
        original = recorder.store.append

        def record_batch(events):
            sizes.append(len(events))
            original(events)

        monkeypatch.setattr(recorder.store, "append", record_batch)
        await recorder.start()
        try:
            # No await: the writer cannot consume until the bounded burst ends.
            for _ in range(10001):
                recorder.control("capacity_fixture")
            assert recorder.snapshot()["dropped"] == 1
            assert recorder.sequence() == 10001
            recorder.control("after_failure")
            assert recorder.snapshot()["dropped"] == 2
            assert recorder.sequence() == 10001
            await recorder.drain()
            assert recorder.snapshot()["last_committed_sequence"] == 10000
            assert len(recorder.store.evidence()) == 10000
            assert sizes and max(sizes) <= 250
            assert sum(sizes) == 10000
        finally:
            await recorder.close()

    asyncio.run(run())


def test_concurrent_reads_observe_one_committed_batch_and_keep_old_snapshot(tmp_path):
    store = EvaluationStore(tmp_path / "concurrent.db")
    store.append([event(1)])
    frozen = store.evidence()
    with ThreadPoolExecutor(max_workers=2) as executor:
        writing = executor.submit(store.append, [event(i) for i in range(2, 502)])
        snapshots = [executor.submit(store.evidence) for _ in range(10)]
        writing.result()
        observed = [snapshot.result() for snapshot in snapshots]
    assert len(frozen) == 1
    assert all(len(snapshot) in (1, 501) for snapshot in observed)
    assert all(
        [item.sequence for item in snapshot] == list(range(1, len(snapshot) + 1))
        for snapshot in observed
    )
    assert len(store.evidence()) == 501


def test_empty_history_request_identity_is_not_provenance():
    with pytest.raises(ValidationError):
        HistoryEvidence(
            request_id="",
            as_of=datetime.now(UTC),
            bars=(),
            complete=False,
            request_sequence=1,
        )


def test_registry_rejects_private_record_fields_before_publication(tmp_path):
    store = EvaluationStore(tmp_path / "private.db")
    report: dict[str, JsonValue] = {
        "report_id": "d" * 64,
        "status": "completed",
        "groups": [],
    }
    with pytest.raises(ValueError):
        store.register(
            report, {"groups": [{"market": "GOLD", "account_id": "private"}]}
        )
    assert store.latest() is None


def test_immutable_registry_rejects_changed_rows_under_existing_identity(tmp_path):
    store = EvaluationStore(tmp_path / "identity.db")
    report: dict[str, JsonValue] = {
        "report_id": "e" * 64,
        "status": "completed",
        "groups": [],
    }
    store.register(report, {"groups": [{"market": "GOLD", "matches": 1}]})
    with pytest.raises(ValueError, match=r"[Ii]mmutable|conflict"):
        store.register(report, {"groups": [{"market": "GOLD", "matches": 2}]})
    rows = store.records("e" * 64, "groups", {}, 0, 25)
    assert rows["rows"] == [["GOLD", 1]]


def test_capture_completeness_survives_new_store_instances(tmp_path):
    """A crashed prefix and known overflow cannot become complete on restart."""
    path = tmp_path / "durable-status.db"
    original = EvaluationStore(path)
    original.append([event()])
    reopened = EvaluationStore(path)
    assert reopened.run_status() == {"test": "open"}
    reopened.mark_run("test", "incomplete", 1)
    assert EvaluationStore(path).run_status() == {"test": "incomplete"}
    assert EvaluationStore(path).evidence() == original.evidence()
    with pytest.raises(ValueError):
        reopened.mark_run("test", "complete_without_proof", 0)
    assert EvaluationStore(path).run_status() == {"test": "incomplete"}


def test_closed_run_state_is_persistent_and_separate_from_evidence(tmp_path):
    path = tmp_path / "closed-status.db"
    store = EvaluationStore(path)
    store.append([event()])
    before = store.evidence()
    store.mark_run("test", "closed", 0)
    assert EvaluationStore(path).run_status() == {"test": "closed"}
    assert EvaluationStore(path).evidence() == before

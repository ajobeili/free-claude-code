"""Neutral instrumentation must leave operating traces unchanged."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from tools import scan_server
from tools.pattern_evaluation_events import QuoteEvidence
from tools.pattern_evaluation_store import EvaluationRecorder
from tools.pattern_trigger import PatternTrigger, ReferenceRange


def test_capture_enabled_disabled_or_failed_preserves_operating_decisions(tmp_path):
    async def run():
        async def history(_):
            return {
                "GOLD": ReferenceRange(110, 90),
                "OIL_CRUDE": ReferenceRange(60, 40),
            }

        traces = []
        for enabled, capacity in ((False, 10000), (True, 10000), (True, 1)):
            recorder = EvaluationRecorder(
                tmp_path / f"capture-{enabled}-{capacity}.db", capacity=capacity
            )
            if enabled:
                await recorder.start()
            decisions = []
            alerts = []

            def observe(event, decisions=decisions, recorder=recorder):
                decisions.append(event)
                recorder.control("observed")

            trigger = PatternTrigger(history, on_alert=alerts.append, on_record=observe)
            await trigger.feed_status(True)
            start = datetime(2026, 11, 4, 9, 10, 10, tzinfo=UTC)
            for epic, mid, second in (
                ("GOLD", 100, 0),
                ("OIL_CRUDE", 50, 1),
                ("GOLD", 100, 2),
                ("GOLD", 114, 3),
                ("OIL_CRUDE", 49, 4),
                ("GOLD", 109, 5),
            ):
                time = start + timedelta(seconds=second)
                await trigger.on_quote(epic, time, time, mid - 0.5, mid + 0.5)
            traces.append((decisions, alerts))
            await recorder.close()
        assert traces[0] == traces[1] == traces[2]
        assert len(traces[0][1]) == 1

    asyncio.run(run())


def test_capture_isolated_from_real_files(tmp_path):
    assert scan_server._PATTERN_EVALUATION.store.path.name == "evaluation.sqlite3"
    assert scan_server._PATTERN_EVALUATION.store.path.parent.parent == tmp_path.parent


def test_evidence_models_reject_private_fields():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        QuoteEvidence.model_validate(
            {"bid": 1, "offer": 2, "quality": "timely", "token": "private"}
        )


def test_feed_capture_runs_inside_detector_serialization_boundary():
    async def run():
        observations = []

        async def history(_):
            return {}

        trigger = PatternTrigger(history, on_feed=observations.append)
        await trigger.feed_status(True)
        await trigger.feed_status(True)
        await trigger.feed_status(False)
        assert observations == [True, False]

    asyncio.run(run())


def test_committed_diagnostics_push_sse_without_another_operating_quote():
    async def run():
        recorder = scan_server._PATTERN_EVALUATION
        await recorder.start()
        response = await scan_server.pattern_events()
        stream = response.body_iterator
        try:
            await anext(stream)
            recorder.control("fixture_event", True)
            await recorder.drain()
            for _ in range(4):
                async with asyncio.timeout(2):
                    message = await anext(stream)
                assert isinstance(message, str)
                payload = json.loads(message.removeprefix("data: "))
                if payload["evaluation_diagnostics"]["processed_through_sequence"] == 1:
                    break
            else:
                raise AssertionError("Committed diagnostics did not push an update")
        finally:
            await stream.aclose()
            await recorder.close()

    asyncio.run(run())

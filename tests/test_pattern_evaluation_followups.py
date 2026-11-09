"""Late observations remain separate from immutable expiry decisions."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools.pattern_evaluation import Evaluator
from tools.pattern_evaluation_events import (
    ControlEvidence,
    Envelope,
    HistoryEvidence,
    QuoteEvidence,
)

START = datetime(2026, 10, 1, 9, 10, 10, tzinfo=UTC)


def breakout_events():
    """The labelled fixture stops immediately after its Gold breakout at t3."""
    return [
        Envelope.model_validate_json(line)
        for line in Path("tests/fixtures/pattern_evaluation/evidence.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if '"run_id":"gold_return"' in line
    ][:8]


def quote(events, second, mid=114, *, received=None, epic="GOLD", run_id="gold_return"):
    time = START + timedelta(seconds=second)
    receipt = START + timedelta(seconds=second if received is None else received)
    events.append(
        Envelope(
            run_id=run_id,
            sequence=sum(event.run_id == run_id for event in events) + 1,
            kind="quote",
            epic=epic,
            source_time=time,
            host_observed_utc=receipt,
            calibrated_received_utc=receipt,
            calibration_id="synthetic-clock-v1",
            payload=QuoteEvidence(bid=mid - 0.5, offer=mid + 0.5, quality="timely"),
        )
    )


def control(events, second, reason, *, connected=None, run_id="gold_return"):
    events.append(
        Envelope(
            run_id=run_id,
            sequence=sum(event.run_id == run_id for event in events) + 1,
            kind="control",
            host_observed_utc=START + timedelta(seconds=second),
            payload=ControlEvidence(reason=reason, connected=connected),
        )
    )


def evaluate(events):
    evaluator = Evaluator(variants=("confirmed-90",))
    for event in events:
        evaluator.process(event)
    return evaluator


def dense_window(*, returned=None, midpoint=109, until=393, missing=()):
    events = breakout_events()
    for second in range(4, until + 1):
        if second not in missing:
            quote(events, second, midpoint if second == returned else 114)
    return events


def test_complete_expiry_window_retains_first_late_return_and_original_expiry():
    evaluator = evaluate(dense_window(returned=100))
    attempt = evaluator.records()["attempts"][0]
    window = evaluator.records()["late_returns"][0]
    assert attempt["outcome"] == "expired"
    assert attempt["return_time"] is None
    assert attempt["deadline"] == (START + timedelta(seconds=93)).isoformat()
    assert window["end"] == (START + timedelta(seconds=393)).isoformat()
    assert window["status"] == "complete"
    assert window["first_return"] == (START + timedelta(seconds=100)).isoformat()
    assert window["elapsed_seconds"] == 97
    assert window["reason"] == "Observed late return"


def test_no_observed_return_requires_complete_window_and_full_fixed_deadline():
    events = dense_window(until=392)
    evaluator = evaluate(events)
    window = evaluator.records()["late_returns"][0]
    assert window["status"] == "pending"
    assert window["first_return"] is None
    quote(events, 393)
    evaluator.process(events[-1])
    window = evaluator.records()["late_returns"][0]
    assert window["status"] == "complete"
    assert window["reason"] == "no_observed_return"


@pytest.mark.parametrize("midpoint", [90, 110])
def test_late_range_boundary_touch_is_not_strict_return(midpoint):
    evaluator = evaluate(dense_window(returned=100, midpoint=midpoint))
    window = evaluator.records()["late_returns"][0]
    assert window["status"] == "complete"
    assert window["first_return"] is None
    assert window["reason"] == "no_observed_return"


@pytest.mark.parametrize("second,observed", [(393, True), (394, False)])
def test_late_return_at_fixed_window_end_is_observed_but_after_end_is_not(
    second, observed
):
    evaluator = evaluate(dense_window(returned=second, until=394))
    window = evaluator.records()["late_returns"][0]
    assert window["status"] == "complete"
    assert (window["first_return"] is not None) == observed


def test_gap_and_observed_late_return_are_independent_facts():
    evaluator = evaluate(dense_window(returned=150, missing=range(100, 110)))
    attempt = evaluator.records()["attempts"][0]
    window = evaluator.records()["late_returns"][0]
    assert attempt["outcome"] == "expired"
    assert window["status"] == "incomplete"
    assert window["first_return"] == (START + timedelta(seconds=150)).isoformat()
    assert window["elapsed_seconds"] == 147
    assert window["reason"] != "no_observed_return"


def test_recording_interruption_keeps_observed_return_but_invalidates_completeness():
    events = dense_window(returned=100, until=199)
    control(events, 200, "recording_failure")
    for second in range(200, 394):
        quote(events, second)
    evaluator = evaluate(events)
    window = evaluator.records()["late_returns"][0]
    assert window["status"] == "incomplete"
    assert window["first_return"] == (START + timedelta(seconds=100)).isoformat()
    assert window["reason"] == "recording_failure"
    assert evaluator.records()["attempts"][0]["outcome"] == "expired"


def test_recording_restart_does_not_turn_existing_expiry_into_match():
    events = dense_window(returned=100, until=199)
    control(events, 200, "server_start", connected=True, run_id="restarted")
    for second in range(200, 394):
        quote(events, second, run_id="restarted")
    evaluator = evaluate(events)
    window = evaluator.records()["late_returns"][0]
    assert window["status"] == "incomplete"
    assert window["first_return"] == (START + timedelta(seconds=100)).isoformat()
    assert evaluator.records()["attempts"][0]["outcome"] == "expired"


def test_expiry_first_observed_after_window_end_cannot_claim_complete_no_return():
    events = breakout_events()
    quote(events, 500, 109)
    evaluator = evaluate(events)
    attempt = evaluator.records()["attempts"][0]
    window = evaluator.records()["late_returns"][0]
    assert attempt["outcome"] == "expired"
    assert attempt["ended"] == (START + timedelta(seconds=500)).isoformat()
    assert window["deadline"] == (START + timedelta(seconds=93)).isoformat()
    assert window["end"] == (START + timedelta(seconds=393)).isoformat()
    assert window["status"] == "incomplete"
    assert window["first_return"] is None
    assert window["reason"] != "no_observed_return"


def test_later_received_older_target_return_does_not_rewrite_first_observation():
    events = dense_window(until=100)
    quote(events, 101, 109)
    quote(events, 99, 109, received=102)
    for second in range(102, 394):
        quote(events, second)
    evaluator = evaluate(events)
    window = evaluator.records()["late_returns"][0]
    assert window["first_return"] == (START + timedelta(seconds=101)).isoformat()
    assert window["elapsed_seconds"] == 98
    assert window["status"] == "complete"
    assert evaluator.records()["attempts"][0]["outcome"] == "expired"


def test_pending_expiry_window_does_not_suppress_newer_attempt_or_its_window():
    events = dense_window(returned=100, until=101)
    # Make exact next-minute histories available; the prior window is independent.
    original = [event for event in breakout_events() if event.kind == "history"]
    request_sequence = len(events) + 1
    control(events, 102, "history_request")
    as_of = START + timedelta(seconds=102)
    for event in original:
        history = event.payload
        assert isinstance(history, HistoryEvidence)
        shift = as_of.replace(second=0, microsecond=0) - history.as_of.replace(
            second=0, microsecond=0
        )
        updated = history.model_copy(
            update={
                "as_of": as_of,
                "request_sequence": request_sequence,
                "request_id": "second-history",
                "bars": tuple(
                    bar.model_copy(update={"time": bar.time + shift})
                    for bar in history.bars
                ),
            }
        )
        events.append(
            Envelope(
                run_id="gold_return",
                sequence=len(events) + 1,
                kind="history",
                epic=event.epic,
                source_time=as_of,
                host_observed_utc=as_of,
                payload=updated,
            )
        )
    quote(events, 103, 50, epic="OIL_CRUDE")
    quote(events, 104, 100)
    quote(events, 105, 114)
    for second in range(106, 496):
        quote(events, second, 109 if second == 200 else 114)
    evaluator = evaluate(events)
    attempts = evaluator.records()["attempts"]
    windows = evaluator.records()["late_returns"]
    assert len(attempts) == 2
    assert [row["outcome"] for row in attempts] == ["expired", "expired"]
    assert len(windows) == 2
    assert windows[0]["first_return"] == (START + timedelta(seconds=100)).isoformat()
    assert windows[1]["first_return"] == (START + timedelta(seconds=200)).isoformat()
    assert windows[0]["end"] == (START + timedelta(seconds=393)).isoformat()
    assert windows[1]["end"] == (START + timedelta(seconds=495)).isoformat()

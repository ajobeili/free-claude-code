"""Frozen definitions, independently hashed snapshots and finalized periods."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from tools.pattern_evaluation_events import (
    MARKETS,
    VARIANTS,
    Bar,
    ControlEvidence,
    Costs,
    Envelope,
    HistoryEvidence,
    Manifest,
    QuoteEvidence,
    Row,
    Session,
)
from tools.pattern_evaluation_report import evaluate, finalization, snapshot

START = datetime(2026, 10, 1, 9, 8, tzinfo=UTC)
SESSION_START = START + timedelta(minutes=20)
END = SESSION_START + timedelta(minutes=1)


def definition(**changes):
    values = {
        "label": "validation",
        "source_id": "capital-demo",
        "frozen_at": datetime(2026, 9, 30, tzinfo=UTC),
        "development_start": datetime(2026, 10, 1, 8, tzinfo=UTC),
        "development_end": datetime(2026, 10, 1, 9, tzinfo=UTC),
        "validation_start": START,
        "validation_end": END,
        "sessions": tuple(
            Session(
                market=market,
                start=SESSION_START,
                end=END,
                provenance="synthetic session source",
            )
            for market in MARKETS
        ),
        "costs": Costs(
            commission=0,
            slippage=0,
            funding=0,
            adjustment=0,
            provenance="synthetic measured components",
            timely_execution=True,
        ),
    }
    values.update(changes)
    return Manifest.model_validate(values)


def finalized_inputs():
    """Trusted derived coverage tests the finalizer, not the replay calculator."""
    events = []

    def add(kind, payload, *, market=None, time=None):
        events.append(
            Envelope(
                run_id="validation-fixture",
                sequence=len(events) + 1,
                kind=kind,
                epic=market,
                source_time=time,
                host_observed_utc=time or START,
                calibrated_received_utc=time if kind == "quote" else None,
                calibration_id="synthetic-clock-v1"
                if kind == "quote"
                or (
                    isinstance(payload, ControlEvidence)
                    and payload.reason == "clock_calibration"
                )
                else None,
                payload=payload,
            )
        )

    add("control", ControlEvidence(reason="feed_connected", connected=True))
    add("control", ControlEvidence(reason="clock_calibration", clock_offset_seconds=0))
    add("control", ControlEvidence(reason="history_request"))
    request_sequence = len(events)
    for market in MARKETS:
        middle = 100 if market == "GOLD" else 50
        bars = tuple(
            Bar(
                time=SESSION_START - timedelta(minutes=index),
                open=middle,
                high=middle + 10,
                low=middle - 10,
                close=middle,
            )
            for index in range(20, 0, -1)
        )
        add(
            "history",
            HistoryEvidence(
                request_id="validation-history",
                as_of=SESSION_START,
                bars=bars,
                complete=True,
                request_sequence=request_sequence,
            ),
            market=market,
            time=SESSION_START,
        )
    for second in range(0, 61, 5):
        time = SESSION_START + timedelta(seconds=second)
        for market in MARKETS:
            middle = 100 if market == "GOLD" else 50
            add(
                "quote",
                QuoteEvidence(bid=middle - 0.5, offer=middle + 0.5, quality="timely"),
                market=market,
                time=time,
            )
    records: dict[str, list[Row]] = {
        kind: []
        for kind in (
            "coverage",
            "stages",
            "attempts",
            "followups",
            "late_returns",
            "groups",
        )
    }
    for market in MARKETS:
        for variant in VARIANTS:
            for paired in (False, True):
                records["coverage"].append(
                    {
                        "run_id": "validation-fixture",
                        "market": market,
                        "variant": variant,
                        "phase": "validation",
                        "start": SESSION_START.isoformat(),
                        "end": END.isoformat(),
                        "paired": paired,
                        "seconds": 60,
                        "sessions_verified": True,
                    }
                )
            records["groups"].append(
                {
                    "market": market,
                    "variant": variant,
                    "phase": "validation",
                    "matches": 0,
                    "observations": 0,
                }
            )
    return tuple(events), records


@pytest.mark.parametrize(
    "changes",
    [
        {"frozen_at": START},
        {"frozen_at": START + timedelta(seconds=1)},
        {"validation_start": START - timedelta(seconds=1)},
        {"validation_end": START},
        {"variants": ("confirmed-90",) * 4},
        {"variants": ("confirmed-90", "unfiltered-90", "confirmed-180")},
        {"variants": (*VARIANTS, "unfiltered-300")},
        {"sessions": ()},
        {"costs": None},
    ],
)
def test_formal_manifest_rejects_retrospective_or_incomplete_method(changes):
    with pytest.raises(ValidationError):
        definition(**changes)


def test_eight_minute_embargo_is_inclusive_and_definition_is_immutable():
    frozen = definition()
    assert frozen.validation_start - frozen.development_end == timedelta(minutes=8)
    with pytest.raises(ValidationError):
        frozen.validation_end = END + timedelta(days=1)
    revised = definition(validation_end=END + timedelta(days=1))
    assert revised.identity() != frozen.identity()


def test_appended_evidence_changes_snapshot_not_prospective_definition():
    frozen = definition()
    evidence, _ = finalized_inputs()
    early = snapshot(frozen, evidence[:10])
    later = snapshot(frozen, evidence)
    assert early.definition_hash == later.definition_hash == frozen.identity()
    assert early.identity() != later.identity()
    assert early.evidence_hash != later.evidence_hash
    assert early.cutoffs[0].sequence == 10
    assert later.cutoffs[0].sequence == len(evidence)
    assert frozen == definition()


def test_inspected_source_hash_cannot_be_relabelled_fresh_validation():
    frozen = definition()
    evidence, _ = finalized_inputs()
    captured = snapshot(frozen, evidence)
    for inspected in (captured.evidence_hash, captured.cutoffs[0].source_hash):
        with pytest.raises(ValueError, match=r"[Ii]nspected"):
            snapshot(definition(inspected_hashes=(inspected,)), evidence)


def test_frozen_source_identity_must_match_captured_evidence():
    evidence, _ = finalized_inputs()
    with pytest.raises(ValueError, match="identity"):
        snapshot(definition(source_id="another-accountless-source"), evidence)


def test_period_cannot_finalize_before_frozen_end_despite_complete_numeric_inputs():
    evidence, records = finalized_inputs()
    # Even an apparent perfect early sample cannot shorten the declared period.
    for group in records["groups"]:
        group.update(
            observations=10000,
            normal_mean=100,
            doubled_mean=99,
            lower_mean=98,
            lower_improvement=97,
        )
    gates = finalization(
        definition(), evidence, records, now=END - timedelta(seconds=1)
    )
    assert gates["period_finalized"] is False
    assert gates["missing"]


def test_complete_shared_zero_match_date_is_retained_after_period_finalizes():
    evidence, records = finalized_inputs()
    gates = finalization(
        definition(), evidence, records, now=END + timedelta(seconds=1)
    )
    assert gates["period_finalized"] is True
    assert gates["session_complete"] is True
    assert gates["history_verified"] is True
    assert gates["capture_complete"] is True
    assert gates["timing_verified"] is True
    assert gates["common_dates"] == ["2026-10-01"]


def test_one_market_incomplete_session_removes_common_date_and_finalization():
    evidence, records = finalized_inputs()
    records["coverage"] = [
        row for row in records["coverage"] if row["market"] != "OIL_CRUDE"
    ]
    gates = finalization(
        definition(), evidence, records, now=END + timedelta(seconds=1)
    )
    assert gates["session_complete"] is False
    assert gates["period_finalized"] is False
    assert gates["common_dates"] == []


def test_missing_period_watermark_blocks_finalization():
    evidence, records = finalized_inputs()
    truncated = evidence[:-2]
    gates = finalization(
        definition(), truncated, records, now=END + timedelta(seconds=1)
    )
    assert gates["period_finalized"] is False


def test_history_without_request_provenance_cannot_be_verified():
    evidence, records = finalized_inputs()
    changed = []
    for event in evidence:
        if isinstance(event.payload, HistoryEvidence):
            event = Envelope.model_validate(
                {
                    **event.model_dump(exclude={"payload", "payload_hash"}),
                    "payload": event.payload.model_copy(update={"request_sequence": 1}),
                }
            )
        changed.append(event)
    gates = finalization(
        definition(), tuple(changed), records, now=END + timedelta(seconds=1)
    )
    assert gates["history_verified"] is False


@pytest.mark.parametrize("problem", ["unproven_clock", "delayed_quote"])
def test_missing_clock_provenance_or_delayed_prices_cannot_prove_timing(problem):
    evidence, records = finalized_inputs()
    changed = []
    for event in evidence:
        if (
            problem == "unproven_clock"
            and isinstance(event.payload, ControlEvidence)
            and event.payload.reason == "clock_calibration"
        ):
            event = event.model_copy(update={"calibration_id": "different-clock"})
        if (
            problem == "delayed_quote"
            and event.kind == "quote"
            and event.source_time is not None
        ):
            event = event.model_copy(
                update={
                    "calibrated_received_utc": event.source_time + timedelta(seconds=6)
                }
            )
        changed.append(event)
    gates = finalization(
        definition(), tuple(changed), records, now=END + timedelta(seconds=10)
    )
    assert gates["timing_verified"] is False


def test_recording_failure_latch_blocks_formal_period_completion():
    evidence, records = finalized_inputs()
    failed = Envelope(
        run_id="validation-fixture",
        sequence=len(evidence) + 1,
        kind="control",
        host_observed_utc=END,
        payload=ControlEvidence(reason="recording_failure"),
    )
    gates = finalization(
        definition(), (*evidence, failed), records, now=END + timedelta(seconds=1)
    )
    assert gates["capture_complete"] is False
    assert gates["period_finalized"] is False


def test_pending_outcome_blocks_finalization_despite_full_period_watermark():
    evidence, records = finalized_inputs()
    records["followups"].append(
        {
            "attempt_id": "pending",
            "market": "GOLD",
            "variant": "confirmed-90",
            "phase": "validation",
            "status": "pending",
            "due": END.isoformat(),
            "horizon_seconds": 180,
        }
    )
    gates = finalization(
        definition(), evidence, records, now=END + timedelta(seconds=1)
    )
    assert gates["period_finalized"] is False


def test_unfinalized_empty_evidence_is_legitimate_insufficient_report():
    report, records = evaluate(definition(), ())
    assert report["status"] == "completed"
    assert len(records["groups"]) == 8
    assert all(group["decision"] == "insufficient" for group in records["groups"])
    comparisons = report["comparisons"]
    assert isinstance(comparisons, list)
    assert all(
        isinstance(item, dict) and item["decision"] == "insufficient"
        for item in comparisons
    )


def synthetic_inputs():
    directory = Path("tests/fixtures/pattern_evaluation")
    frozen = Manifest.model_validate_json(
        (directory / "manifest.json").read_text(encoding="utf-8")
    )
    evidence = tuple(
        Envelope.model_validate_json(line)
        for line in (directory / "evidence.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if '"run_id":"gold_return"' in line
    )
    return frozen, evidence


def test_phase_local_history_cannot_borrow_bars_from_prior_phase():
    frozen, evidence = synthetic_inputs()
    late_start = datetime(2026, 10, 1, 9, 10, tzinfo=UTC)
    frozen = Manifest.model_validate(
        {**frozen.model_dump(), "development_start": late_start}
    )
    _, records = evaluate(frozen, evidence)
    assert records["attempts"] == []


def test_full_attempt_and_outcome_windows_must_fit_declared_phase():
    frozen, evidence = synthetic_inputs()
    end = datetime(2026, 10, 1, 9, 11, tzinfo=UTC)
    frozen = Manifest.model_validate({**frozen.model_dump(), "development_end": end})
    report, records = evaluate(frozen, evidence)
    assert report["phase"] == "development"
    assert records["attempts"]
    assert all(row.get("phase_eligible") is False for row in records["attempts"])
    assert records["followups"]
    assert all(row.get("phase_eligible") is False for row in records["followups"])
    assert all(row["status"] == "incomplete" for row in records["followups"])
    assert "NaN" not in json.dumps(report, allow_nan=False)

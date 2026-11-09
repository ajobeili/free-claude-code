"""Causal baseline parity against the operating detector, without broker access."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools.pattern_evaluation import Evaluator
from tools.pattern_evaluation_events import (
    VARIANTS,
    Bar,
    ControlEvidence,
    Envelope,
    HistoryEvidence,
    Market,
    OperatingDecision,
    QuoteEvidence,
)
from tools.pattern_events import AttemptRecord, DecisionRecord, GapRecord, QuoteRecord
from tools.pattern_trigger import PatternTrigger, ReferenceRange

START = datetime(2026, 11, 4, 9, 10, 10, tzinfo=UTC)
RANGES = {"GOLD": (110, 90), "OIL_CRUDE": (60, 40)}


class CaptureHarness:
    """Capture only evidence available to the detector at each callback."""

    def __init__(self, history_markets=("GOLD", "OIL_CRUDE")):
        self.events: list[Envelope] = []
        self.starts: list[AttemptRecord] = []
        self.decisions: list[DecisionRecord] = []
        self.alerts = []
        self.history_markets = history_markets
        self.observed = START
        self.trigger = PatternTrigger(
            self.history, on_record=self.record, on_alert=self.alerts.append
        )

    def emit(self, kind, payload, *, epic=None, source=None, received=None):
        event = Envelope(
            run_id="baseline-parity",
            sequence=len(self.events) + 1,
            kind=kind,
            epic=epic,
            source_time=source,
            host_observed_utc=self.observed,
            calibrated_received_utc=received,
            calibration_id="synthetic-clock-v1" if received else None,
            payload=payload,
        )
        self.events.append(event)

    async def connected(self, value):
        self.emit("control", ControlEvidence(reason="feed", connected=value))
        await self.trigger.feed_status(value)

    async def history(self, as_of):
        self.emit("control", ControlEvidence(reason="history_request"))
        request_sequence = len(self.events)
        minute = as_of.replace(second=0, microsecond=0)
        result = {}
        for epic in self.history_markets:
            high, low = RANGES[epic]
            bars = tuple(
                Bar(
                    time=minute - timedelta(minutes=index),
                    open=(high + low) / 2,
                    high=high,
                    low=low,
                    close=(high + low) / 2,
                )
                for index in range(20, 0, -1)
            )
            self.emit(
                "history",
                HistoryEvidence(
                    request_id=f"history-{request_sequence}",
                    as_of=as_of,
                    bars=bars,
                    complete=True,
                    request_sequence=request_sequence,
                ),
                epic=epic,
                source=as_of,
            )
            result[epic] = ReferenceRange(high, low)
        return result

    def record(self, record):
        if isinstance(record, QuoteRecord):
            self.emit(
                "quote",
                QuoteEvidence(
                    bid=record.bid, offer=record.offer, quality=record.quality
                ),
                epic=record.epic,
                source=record.time,
                received=record.received,
            )
        elif isinstance(record, AttemptRecord):
            self.starts.append(record)
        elif isinstance(record, DecisionRecord):
            self.decisions.append(record)
            self.emit(
                "decision",
                OperatingDecision(started=record.started, outcome=record.outcome),
                epic=record.epic,
                source=record.time,
            )
        elif isinstance(record, GapRecord):
            self.emit("control", ControlEvidence(reason=record.reason))

    async def tick(self, epic: Market, midpoint, second, *, received=None, spread=1):
        time = START + timedelta(seconds=second)
        receipt = START + timedelta(seconds=second if received is None else received)
        self.observed = receipt
        await self.trigger.on_quote(
            epic, time, receipt, midpoint - spread / 2, midpoint + spread / 2
        )

    async def prepared(self):
        await self.connected(True)
        await self.tick("GOLD", 100, 0)
        await self.tick("OIL_CRUDE", 50, 1)
        await self.tick("GOLD", 100, 2)

    def replay(self):
        evaluator = Evaluator(variants=("confirmed-90",))
        for event in self.events:
            evaluator.process(event)
        return evaluator

    def assert_parity(self):
        evaluator = self.replay()
        rows = evaluator.records()["attempts"]
        expected_starts = [
            (item.epic, item.started.isoformat()) for item in self.starts
        ]
        actual_starts = [(row["market"], row["started"]) for row in rows]
        assert actual_starts == expected_starts
        expected_terminals = [
            (
                item.epic,
                item.started.isoformat(),
                "matched" if item.outcome == "qualified" else item.outcome,
            )
            for item in self.decisions
        ]
        actual_terminals = [
            (row["market"], row["started"], row["outcome"])
            for row in rows
            if row["outcome"] not in ("open", "interrupted")
        ]
        assert actual_terminals == expected_terminals
        assert evaluator.parity() != "baseline_mismatch"
        return evaluator


def scenario_test(scenario: Callable):
    async def run():
        capture = CaptureHarness()
        await capture.prepared()
        await scenario(capture)
        capture.assert_parity()
        return capture

    return asyncio.run(run())


@pytest.mark.parametrize("returned", [5, 93])
def test_baseline_return_and_exact_deadline_match_in_receipt_order(returned):
    async def scenario(capture):
        await capture.tick("GOLD", 114, 3)
        await capture.tick("OIL_CRUDE", 49, returned - 1)
        await capture.tick("GOLD", 109, returned)

    capture = scenario_test(scenario)
    assert [item.outcome for item in capture.decisions] == ["qualified"]


def test_cross_market_expiry_is_terminal_before_later_received_deadline_quote():
    async def scenario(capture):
        await capture.tick("GOLD", 114, 3)
        await capture.tick("OIL_CRUDE", 49, 94)
        await capture.tick("GOLD", 109, 93, received=95)

    capture = scenario_test(scenario)
    assert [item.outcome for item in capture.decisions] == ["expired"]
    assert capture.replay().records()["attempts"][0]["outcome"] == "expired"


def test_minute_reset_clears_inside_and_refresh_quote_never_starts():
    async def scenario(capture):
        await capture.tick("OIL_CRUDE", 50, 49)
        await capture.tick("GOLD", 114, 50)
        await capture.tick("OIL_CRUDE", 50, 51)
        await capture.tick("GOLD", 114, 52)
        assert capture.starts == []
        await capture.tick("GOLD", 100, 53)
        await capture.tick("OIL_CRUDE", 50, 54)
        await capture.tick("GOLD", 114, 55)

    capture = scenario_test(scenario)
    assert len(capture.starts) == 1
    assert capture.starts[0].started == START + timedelta(seconds=55)


def test_pending_frozen_range_survives_minute_refresh_by_other_market():
    async def scenario(capture):
        await capture.tick("GOLD", 114, 3)
        await capture.tick("OIL_CRUDE", 49, 51)
        await capture.tick("GOLD", 109, 52)

    capture = scenario_test(scenario)
    assert [item.outcome for item in capture.decisions] == ["qualified"]
    assert capture.starts[0].range_high == 110


def test_coupled_history_blocks_baseline_when_only_target_history_available():
    async def run():
        capture = CaptureHarness(history_markets=("GOLD",))
        await capture.prepared()
        await capture.tick("OIL_CRUDE", 50, 3)
        await capture.tick("GOLD", 100, 4)
        await capture.tick("GOLD", 114, 5)
        capture.assert_parity()
        assert capture.starts == []

    asyncio.run(run())


def test_latest_received_counterpart_is_used_even_if_earlier_one_would_confirm():
    async def scenario(capture):
        await capture.tick("GOLD", 114, 3)
        await capture.tick("OIL_CRUDE", 49, 4)
        await capture.tick("OIL_CRUDE", 51, 6)
        await capture.tick("GOLD", 109, 5, received=7)

    capture = scenario_test(scenario)
    assert [item.outcome for item in capture.decisions] == ["context_failed"]


def test_failed_confirmation_consumes_attempt_without_starting_cooldown():
    async def scenario(capture):
        await capture.tick("GOLD", 114, 3)
        await capture.tick("OIL_CRUDE", 51, 4)
        await capture.tick("GOLD", 109, 5)
        await capture.tick("OIL_CRUDE", 49, 6)
        await capture.tick("GOLD", 109, 7)
        assert len(capture.decisions) == 1
        await capture.tick("GOLD", 114, 8)
        await capture.tick("OIL_CRUDE", 48, 9)
        await capture.tick("GOLD", 109, 10)

    capture = scenario_test(scenario)
    assert [item.outcome for item in capture.decisions] == [
        "context_failed",
        "qualified",
    ]


def test_only_matched_attempt_starts_ninety_second_cooldown():
    async def scenario(capture):
        await capture.tick("GOLD", 114, 3)
        await capture.tick("OIL_CRUDE", 49, 4)
        await capture.tick("GOLD", 109, 5)
        await capture.tick("GOLD", 100, 6)
        await capture.tick("GOLD", 114, 7)
        assert len(capture.starts) == 1
        # A later minute refresh must complete, followed by a new inside observation.
        await capture.tick("OIL_CRUDE", 49, 95)
        await capture.tick("GOLD", 100, 96)
        await capture.tick("GOLD", 114, 97)

    capture = scenario_test(scenario)
    assert len(capture.starts) == 2
    assert capture.starts[1].started == START + timedelta(seconds=97)


def test_explicit_baseline_disagreement_blocks_comparison():
    async def scenario(capture):
        await capture.tick("GOLD", 114, 3)
        await capture.tick("OIL_CRUDE", 49, 4)
        await capture.tick("GOLD", 109, 5)

    capture = scenario_test(scenario)
    evaluator = capture.replay()
    evaluator.process(
        Envelope(
            run_id="baseline-parity",
            sequence=len(capture.events) + 1,
            kind="decision",
            epic="GOLD",
            source_time=START + timedelta(seconds=5),
            host_observed_utc=START + timedelta(seconds=5),
            payload=OperatingDecision(
                started=START + timedelta(seconds=3), outcome="expired"
            ),
        )
    )
    assert evaluator.parity() == "baseline_mismatch"


def test_disconnection_interrupts_attempt_instead_of_inventing_terminal_return():
    async def scenario(capture):
        await capture.tick("GOLD", 114, 3)
        await capture.connected(False)
        await capture.connected(True)
        await capture.tick("OIL_CRUDE", 49, 4)
        await capture.tick("GOLD", 109, 5)

    capture = scenario_test(scenario)
    rows = capture.replay().records()["attempts"]
    assert len(rows) == 1
    assert rows[0]["outcome"] == "interrupted"


def test_missing_history_is_unknown_in_diagnostics_not_zero_success():
    async def run():
        capture = CaptureHarness(history_markets=())
        await capture.connected(True)
        await capture.tick("GOLD", 100, 0)
        await capture.tick("GOLD", 114, 1)
        evaluator = capture.assert_parity()
        rows = evaluator.records()["stages"]
        assert any(row.get("state") == "unknown" for row in rows)
        assert evaluator.records()["attempts"] == []

    asyncio.run(run())


def test_stale_quote_recovery_keeps_same_minute_reference_without_new_history():
    async def scenario(capture):
        await capture.tick("GOLD", 114, 3)
        # The operating detector cancels the attempt but keeps loaded ranges.
        await capture.tick("GOLD", 109, 4, received=35)
        histories_before = sum(event.kind == "history" for event in capture.events)
        await capture.tick("OIL_CRUDE", 50, 36)
        await capture.tick("GOLD", 100, 37)
        await capture.tick("GOLD", 114, 38)
        await capture.tick("OIL_CRUDE", 49, 39)
        await capture.tick("GOLD", 109, 40)
        assert (
            sum(event.kind == "history" for event in capture.events) == histories_before
        )

    capture = scenario_test(scenario)
    assert len(capture.starts) == 2
    assert [row["outcome"] for row in capture.replay().records()["attempts"]] == [
        "interrupted",
        "matched",
    ]


def fixture_events():
    return [
        Envelope.model_validate_json(line)
        for line in Path("tests/fixtures/pattern_evaluation/evidence.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def test_all_eight_groups_reconcile_labelled_variant_fixture_terminals():
    evaluator = Evaluator()
    for event in fixture_events():
        evaluator.process(event)
    expected = json.loads(
        Path("tests/fixtures/pattern_evaluation/expected.json").read_text(
            encoding="utf-8"
        )
    )
    attempts = evaluator.records()["attempts"]
    for case in expected["cases"]:
        for variant, terminal in case["terminal_by_variant"].items():
            selected = [
                row
                for row in attempts
                if row["run_id"] == case["run_id"]
                and row["market"] == case["market"]
                and row["variant"] == variant
            ]
            if terminal is None:
                assert selected == [], (case["run_id"], variant)
            else:
                assert selected[0]["outcome"] == terminal, (case["run_id"], variant)
    groups = evaluator.records()["groups"]
    assert {(row["market"], row["variant"]) for row in groups} == {
        (market, variant) for market in ("GOLD", "OIL_CRUDE") for variant in VARIANTS
    }


def dense_return_events(*, breakout_sign=1, last_second=305):
    """Pure invented evidence with one-second outcomes, including all horizons."""
    base = [event for event in fixture_events() if event.run_id == "gold_return"]
    events = []
    for event in base:
        if (
            event.source_time is not None
            and event.kind == "quote"
            and event.source_time > datetime(2026, 10, 1, 9, 10, 15, tzinfo=UTC)
        ):
            continue
        if breakout_sign == -1 and event.kind == "quote":
            quote = event.payload
            assert isinstance(quote, QuoteEvidence)
            # Mirror synthetic prices around each market midpoint.
            center = 100 if event.epic == "GOLD" else 50
            event = Envelope.model_validate(
                {
                    **event.model_dump(exclude={"payload_hash", "payload"}),
                    "payload": QuoteEvidence(
                        bid=2 * center - quote.offer,
                        offer=2 * center - quote.bid,
                        quality="timely",
                    ),
                }
            )
        events.append(event)
    start = datetime(2026, 10, 1, 9, 10, 10, tzinfo=UTC)
    middle = 108 if breakout_sign == 1 else 92
    for second in range(6, last_second + 1):
        time = start + timedelta(seconds=second)
        events.append(
            Envelope(
                run_id="gold_return",
                sequence=len(events) + 1,
                kind="quote",
                epic="GOLD",
                source_time=time,
                host_observed_utc=time,
                calibrated_received_utc=time,
                calibration_id="synthetic-clock-v1",
                payload=QuoteEvidence(
                    bid=middle - 0.5, offer=middle + 0.5, quality="timely"
                ),
            )
        )
    return events


@pytest.mark.parametrize(
    "breakout_sign,entry,exit_value", [(1, 108.5, 108.5), (-1, 91.5, 91.5)]
)
def test_all_variants_have_quote_side_complete_one_three_five_minute_measurements(
    breakout_sign, entry, exit_value
):
    evaluator = Evaluator()
    for event in dense_return_events(breakout_sign=breakout_sign):
        evaluator.process(event)
    measurements = evaluator.records()["followups"]
    assert len(measurements) == 12
    assert {(row["variant"], row["horizon_seconds"]) for row in measurements} == {
        (variant, horizon) for variant in VARIANTS for horizon in (60, 180, 300)
    }
    for row in measurements:
        assert row["status"] == "complete"
        assert row["direction"] == -breakout_sign
        assert row["entry"] == entry
        assert row["exit"] == exit_value
        assert row["points"] == 0
        assert row["entry_spread"] == row["exit_spread"] == 1
        assert row["best"] == 0
        assert row["worst"] == -1
        assert row["eligible_primary_match"] == (row["horizon_seconds"] == 180)


def test_future_measurements_never_rewrite_terminal_attempts_or_cooldown():
    evaluator = Evaluator()
    events = dense_return_events()
    for event in events[:10]:
        evaluator.process(event)
    # The first ten records include the return that ends the original attempts.
    before = evaluator.records()["attempts"]
    assert before and all(row["outcome"] == "matched" for row in before)
    for event in events[10:]:
        evaluator.process(event)
    assert evaluator.records()["attempts"] == before
    assert all(row["status"] == "complete" for row in evaluator.records()["followups"])


@pytest.mark.parametrize("midpoint", [110, 112, 90, 88])
def test_range_and_exact_two_spread_threshold_touches_do_not_start(midpoint):
    events = dense_return_events(last_second=5)
    # Keep the prepared observations, replacing the original breakout/return tail.
    events = events[:6]
    start = datetime(2026, 10, 1, 9, 10, 10, tzinfo=UTC)
    for epic, mid, second in (("GOLD", 100, 2), ("GOLD", midpoint, 3)):
        time = start + timedelta(seconds=second)
        events.append(
            Envelope(
                run_id="gold_return",
                sequence=len(events) + 1,
                kind="quote",
                epic=epic,
                source_time=time,
                host_observed_utc=time,
                calibrated_received_utc=time,
                payload=QuoteEvidence(bid=mid - 0.5, offer=mid + 0.5, quality="timely"),
            )
        )
    evaluator = Evaluator()
    for event in events:
        evaluator.process(event)
    assert evaluator.records()["attempts"] == []


def test_failed_confirmed_attempt_and_matched_unfiltered_have_independent_cooldowns():
    events = [event for event in fixture_events() if event.run_id == "wrong_direction"][
        :10
    ]
    start = datetime(2026, 10, 1, 9, 10, 10, tzinfo=UTC)
    for epic, mid, second in (("GOLD", 109, 6), ("OIL_CRUDE", 51, 7), ("GOLD", 114, 8)):
        time = start + timedelta(seconds=second)
        events.append(
            Envelope(
                run_id="wrong_direction",
                sequence=len(events) + 1,
                kind="quote",
                epic=epic,
                source_time=time,
                host_observed_utc=time,
                calibrated_received_utc=time,
                payload=QuoteEvidence(bid=mid - 0.5, offer=mid + 0.5, quality="timely"),
            )
        )
    evaluator = Evaluator()
    for event in events:
        evaluator.process(event)
    attempts = evaluator.records()["attempts"]
    for variant in VARIANTS:
        selected = [row for row in attempts if row["variant"] == variant]
        assert len(selected) == (2 if variant.startswith("confirmed") else 1)


def test_sparse_horizon_quotes_are_incomplete_instead_of_zero_movements():
    evaluator = Evaluator()
    for event in fixture_events():
        if event.run_id == "gold_return":
            evaluator.process(event)
    rows = evaluator.records()["followups"]
    assert len(rows) == 12
    assert all(row["status"] == "incomplete" and row["points"] is None for row in rows)

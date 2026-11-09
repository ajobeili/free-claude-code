"""Regenerate entirely synthetic causal-order evaluation fixtures."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tools.pattern_evaluation_events import Envelope

ROOT = Path(__file__).parent
START = datetime(2026, 10, 1, 9, 10, 10, tzinfo=UTC)
VARIANTS = ["confirmed-90", "unfiltered-90", "confirmed-180", "unfiltered-180"]


def stamp(seconds=0):
    return (START + timedelta(seconds=seconds)).isoformat()


def scenario(
    name,
    *,
    target="GOLD",
    returned=5,
    direction=-1,
    absent=False,
    no_signal=False,
    cross_expiry=False,
    gap=False,
):
    result = []

    def add(kind, payload, seconds=0, epic=None, received=None):
        result.append(
            {
                "schema_version": 1,
                "run_id": name,
                "sequence": len(result) + 1,
                "kind": kind,
                "epic": epic,
                "source_time": stamp(seconds) if kind != "control" else None,
                "host_observed_utc": stamp(seconds if received is None else received),
                "calibrated_received_utc": stamp(
                    seconds if received is None else received
                ),
                "calibration_id": "synthetic-clock-v1",
                "payload": payload,
            }
        )

    def quote(epic, midpoint, seconds, received=None):
        add(
            "quote",
            {"bid": midpoint - 0.5, "offer": midpoint + 0.5, "quality": "timely"},
            seconds,
            epic,
            received,
        )

    other = "OIL_CRUDE" if target == "GOLD" else "GOLD"
    middle = {"GOLD": 100, "OIL_CRUDE": 50}
    add("control", {"reason": "feed_connected", "connected": True})
    quote(target, middle[target], 0)
    add("control", {"reason": "history_request", "connected": None})
    request_sequence = len(result)
    minute = START.replace(second=0, microsecond=0)
    for epic in ("GOLD", "OIL_CRUDE"):
        bars = [
            {
                "time": (minute - timedelta(minutes=i)).isoformat(),
                "open": middle[epic],
                "high": middle[epic] + 10,
                "low": middle[epic] - 10,
                "close": middle[epic],
            }
            for i in range(20, 0, -1)
        ]
        add(
            "history",
            {
                "request_id": name + "-history",
                "as_of": stamp(),
                "bars": bars,
                "complete": True,
                "request_sequence": request_sequence,
            },
            0,
            epic,
        )
    if not absent:
        quote(other, middle[other], 1)
    quote(target, middle[target], 2)
    if no_signal:
        quote(target, middle[target] + 1, 3)
        quote(other, middle[other] - 1, 4)
        return result
    quote(target, middle[target] + 14, 3)
    if gap:
        add("control", {"reason": "recording_interruption", "connected": False}, 4)
        add("control", {"reason": "feed_connected", "connected": True}, 5)
    if not absent:
        quote(other, middle[other] + direction, 94 if cross_expiry else returned - 1)
    quote(target, middle[target] + 9, returned, 95 if cross_expiry else returned)
    # Follow-up points intentionally sparse: outcomes must be incomplete, never zero.
    for seconds in (returned + 60, returned + 180, returned + 300):
        quote(target, middle[target] + 8, seconds)
    return result


def main():
    cases = [
        ("gold_return", {}, ["matched"] * 4),
        ("oil_return", {"target": "OIL_CRUDE"}, ["matched"] * 4),
        ("exact_90", {"returned": 93}, ["matched"] * 4),
        ("exact_180", {"returned": 183}, ["expired", "expired", "matched", "matched"]),
        ("late_120", {"returned": 123}, ["expired", "expired", "matched", "matched"]),
        ("no_signal", {"no_signal": True}, [None] * 4),
        ("missing_counterpart", {"absent": True}, [None, "matched", None, "matched"]),
        (
            "wrong_direction",
            {"direction": 1},
            ["context_failed", "matched", "context_failed", "matched"],
        ),
        (
            "receipt_order_expiry",
            {"returned": 93, "cross_expiry": True},
            ["expired", "expired", "context_failed", "matched"],
        ),
        ("interruption", {"gap": True}, ["interrupted"] * 4),
        ("late_after_180", {"returned": 184}, ["expired"] * 4),
    ]
    evidence = []
    expected = {
        "label": "synthetic",
        "description": "Invented prices, not a backtest of a traded market.",
        "cases": [],
    }
    for name, arguments, outcomes in cases:
        records = scenario(name, **arguments)
        evidence.extend(records)
        expected["cases"].append(
            {
                "run_id": name,
                "market": arguments.get("target", "GOLD"),
                "starts_at_seconds": None if arguments.get("no_signal") else 3,
                "terminal_by_variant": dict(zip(VARIANTS, outcomes, strict=True)),
                "followups": "incomplete: deliberately sparse target quotes",
                "notes": "Receipt order wins over source timestamps."
                if name == "receipt_order_expiry"
                else name,
            }
        )
    manifest = {
        "schema_version": 1,
        "label": "synthetic",
        "source_id": "invented-gold-oil-fixtures-v1",
        "capture_version": "evaluation-v1",
        "frozen_at": "2026-09-30T00:00:00+00:00",
        "development_start": "2026-10-01T08:00:00+00:00",
        "development_end": "2026-10-01T12:00:00+00:00",
        "validation_start": None,
        "validation_end": None,
        "variants": VARIANTS,
        "inspected_hashes": [],
        "sessions": [],
        "costs": None,
    }
    (ROOT / "evidence.jsonl").write_text(
        "".join(
            Envelope.model_validate(row).model_dump_json() + "\n" for row in evidence
        ),
        encoding="utf-8",
    )
    for name, value in (("manifest.json", manifest), ("expected.json", expected)):
        (ROOT / name).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

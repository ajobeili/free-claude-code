"""Explicit offline evaluation and atomic local publication; never places orders."""

import argparse
import csv
import hashlib
import html
import io
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean, quantiles

from pydantic import JsonValue, ValidationError

from tools.pattern_evaluation import Evaluator, time_of
from tools.pattern_evaluation_events import (
    MARKETS,
    RULE_VERSION,
    VARIANTS,
    ControlEvidence,
    Envelope,
    EvidenceSnapshot,
    HistoryEvidence,
    Manifest,
    QuoteEvidence,
    Row,
    RunCutoff,
    digest,
)
from tools.pattern_evaluation_statistics import GateEvidence, Observation, assess
from tools.pattern_evaluation_store import JSON, EvaluationStore


def store_path() -> Path:
    """Same environment/default as config.Settings, without loading credentials."""
    return Path(
        os.environ.get(
            "PATTERN_EVALUATION_STORE",
            str(Path(__file__).with_name(".pattern_evaluation.sqlite3")),
        )
    )


def read_evidence(
    path: Path, definition: Manifest
) -> tuple[tuple[Envelope, ...], dict[str, str] | None]:
    if path.suffix not in (".db", ".sqlite3"):
        return tuple(
            Envelope.model_validate_json(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ), None
    # Inspect legacy journals read-only. Never create/migrate tables in them or
    # invent missing quote/history availability from independent row IDs.
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "evidence" in tables:
            store = EvaluationStore(path)
            return store.evidence(), store.run_status()
        if definition.label == "validation":
            raise ValueError("Legacy provenance cannot certify formal validation")
        counts = {
            name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in (
                "quotes",
                "attempts",
                "outcomes",
                "reference_ranges",
                "gaps",
                "runs",
            )
            if name in tables
        }
        if not counts:
            raise ValueError("Unknown evidence database schema")
        identity = digest(
            {
                "legacy_counts": counts,
                "content_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
        event = Envelope(
            run_id="legacy-" + identity[:24],
            sequence=1,
            kind="control",
            source_id="legacy-unverified",
            host_observed_utc=definition.development_start,
            payload=ControlEvidence(
                reason="legacy_partial_import:"
                + identity
                + ":"
                + json.dumps(counts, sort_keys=True)
            ),
        )
        return (event,), {event.run_id: "incomplete"}
    finally:
        connection.close()


def snapshot(
    definition: Manifest,
    evidence: tuple[Envelope, ...],
    capture_states: dict[str, str] | None = None,
) -> EvidenceSnapshot:
    runs: dict[str, list[Envelope]] = defaultdict(list)
    for event in evidence:
        runs[event.run_id].append(event)
    cutoffs = []
    for run, events in runs.items():
        if [event.sequence for event in events] != list(range(1, len(events) + 1)):
            raise ValueError("Missing/duplicate/reordered causal sequences")
        cutoffs.append(
            RunCutoff(
                run_id=run,
                sequence=events[-1].sequence,
                source_hash=digest([event.model_dump(mode="json") for event in events]),
            )
        )
    evidence_hash = digest([event.model_dump(mode="json") for event in evidence])
    if definition.label == "validation":
        if evidence_hash in definition.inspected_hashes:
            raise ValueError("Inspected evidence cannot be fresh validation")
        for cutoff in cutoffs:
            if cutoff.source_hash in definition.inspected_hashes and any(
                event.run_id == cutoff.run_id
                and event.source_time is not None
                and definition.validation_start is not None
                and event.source_time >= definition.validation_start
                for event in evidence
            ):
                raise ValueError("Validation overlaps inspected source hashes")
        if any(
            event.source_id != definition.source_id
            or event.capture_version != definition.capture_version
            for event in evidence
        ):
            raise ValueError(
                "Evidence source/capture identity differs from frozen definition"
            )
    times = [
        event.source_time
        for event in evidence
        if event.kind == "quote" and event.source_time is not None
    ]
    return EvidenceSnapshot(
        definition_hash=definition.identity(),
        evidence_hash=evidence_hash,
        cutoffs=tuple(cutoffs),
        observed_through=max(times) if times else None,
        source_ids=tuple(sorted({event.source_id for event in evidence})),
        capture_states=tuple(sorted((capture_states or {}).items())),
    )


def _phase(
    definition: Manifest, evidence: tuple[Envelope, ...], phase: str
) -> tuple[dict[str, list[Row]], str]:
    start = (
        definition.development_start
        if phase == "development"
        else definition.validation_start
    )
    end = (
        definition.development_end
        if phase == "development"
        else definition.validation_end
    )
    if start is None or end is None:
        raise ValueError("Missing phase bounds")
    evaluator = Evaluator()
    # Preserve original per-run sequence. Out-of-phase evidence is not silently
    # renumbered; the reducer observes controls and a phase-local filtered feed.
    selected = []
    for event in evidence:
        available = (
            event.calibrated_received_utc
            or event.source_time
            or event.host_observed_utc
        )
        if (event.source_time or available) > end:
            continue
        if event.kind == "control":
            selected.append(event)
        elif event.source_time is not None and start <= event.source_time <= end:
            if isinstance(event.payload, HistoryEvidence) and any(
                bar.time < start for bar in event.payload.bars
            ):
                continue
            selected.append(event)
    # Filtered phase-local streams carry the original identity in rows. Skipped
    # observations are continuity controls, not fabricated prices/history.
    selected_ids = {(event.run_id, event.sequence) for event in selected}
    for event in evidence:
        if (event.run_id, event.sequence) in selected_ids:
            evaluator.process(event)
        else:
            evaluator.skip(event)
    records = evaluator.records()
    attempt_map = {row["attempt_id"]: row for row in records["attempts"]}
    for kind, rows in records.items():
        for row in rows:
            row["phase"] = phase
        if kind == "attempts":
            for row in rows:
                row["phase_eligible"] = (
                    start <= time_of(row, "started") and time_of(row, "deadline") <= end
                )
        if kind == "followups":
            for row in rows:
                attempt = attempt_map.get(row["attempt_id"])
                contained = (
                    attempt is not None
                    and time_of(attempt, "started") >= start
                    and time_of(row, "due") <= end
                    and time_of(attempt, "deadline") <= end
                    and (
                        row.get("exit_time") is None or time_of(row, "exit_time") <= end
                    )
                )
                row["phase_eligible"] = contained
                if not contained or row["status"] == "pending":
                    row.update(
                        status="incomplete",
                        reason="Phase boundary or snapshot lacks full outcome window",
                    )
        if kind == "late_returns":
            for row in rows:
                if time_of(row, "end") > end or row["status"] == "pending":
                    row.update(
                        status="incomplete",
                        reason="Phase boundary or snapshot lacks full late window",
                    )
    return records, evaluator.parity()


def _covers(
    intervals: list[tuple[datetime, datetime]], start: datetime, end: datetime
) -> bool:
    cursor = start
    for left, right in sorted(intervals):
        if right <= cursor:
            continue
        if left > cursor:
            return False
        cursor = max(cursor, right)
        if cursor >= end:
            return True
    return False


def finalization(
    definition: Manifest,
    evidence: tuple[Envelope, ...],
    records: dict[str, list[Row]],
    now: datetime | None = None,
) -> dict[str, JsonValue]:
    """Derive period/session/timing prerequisites from actual evidence, not flags."""
    now = now or datetime.now(UTC)
    start, end = definition.validation_start, definition.validation_end
    missing = []
    if start is None or end is None:
        return {
            "period_finalized": False,
            "common_dates": [],
            "missing": ["No prospective validation period"],
            "session_complete": False,
            "history_verified": False,
            "timing_verified": False,
            "parity_verified": False,
            "capture_complete": False,
        }
    sessions = {
        market: [session for session in definition.sessions if session.market == market]
        for market in MARKETS
    }
    expected: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
    for gold in sessions["GOLD"]:
        for oil in sessions["OIL_CRUDE"]:
            left, right = (
                max(start, gold.start, oil.start).astimezone(UTC),
                min(end, gold.end, oil.end).astimezone(UTC),
            )
            while left < right:
                midnight = (left + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                boundary = min(right, midnight)
                expected[left.astimezone(UTC).date().isoformat()].append(
                    (left, boundary)
                )
                left = boundary
    intervals = {
        market: [
            (time_of(row, "start"), time_of(row, "end"))
            for row in records.get("coverage", [])
            if row.get("market") == market
            and row.get("variant") == "confirmed-90"
            and row.get("paired") is True
        ]
        for market in MARKETS
    }
    common_dates = [
        day
        for day, windows in sorted(expected.items())
        if all(
            _covers(intervals[market], left, right)
            for market in MARKETS
            for left, right in windows
        )
    ]
    session_complete = bool(expected) and len(common_dates) == len(expected)
    if not session_complete:
        missing.append("Complete sourced shared-session coverage is unavailable")
    selected = [
        event
        for event in evidence
        if event.source_time is not None and start <= event.source_time <= end
    ]
    quotes = [
        event
        for event in selected
        if isinstance(event.payload, QuoteEvidence)
        and event.payload.quality in ("timely", "delayed_research")
    ]
    watermarks = {
        market: max(
            (
                event.source_time
                for event in quotes
                if event.epic == market and event.source_time is not None
            ),
            default=start,
        )
        for market in MARKETS
    }
    through = all(time >= end for time in watermarks.values())
    if now < end:
        missing.append("Frozen validation period has not ended; progress only")
    if not through:
        missing.append("Both market source watermarks must reach the frozen end")
    calibrations = {
        event.calibration_id
        for event in evidence
        if isinstance(event.payload, ControlEvidence)
        and event.payload.reason == "clock_calibration"
        and event.payload.clock_offset_seconds is not None
        and event.calibration_id is not None
    }
    timing_verified = bool(quotes) and all(
        event.calibration_id in calibrations
        and event.calibrated_received_utc is not None
        and event.source_time is not None
        and -1
        <= (event.calibrated_received_utc - event.source_time).total_seconds()
        <= 5
        and event.host_observed_utc <= now
        for event in quotes
    )
    if not timing_verified:
        missing.append("Timely calibrated observations/execution evidence are missing")
    lookup = {(event.run_id, event.sequence): event for event in evidence}
    histories = [
        event
        for event in selected
        if isinstance(event.payload, HistoryEvidence)
        and event.payload.complete
        and all(bar.time >= start for bar in event.payload.bars)
    ]
    history_verified = bool(histories)
    for event in histories:
        payload = event.payload
        if not isinstance(payload, HistoryEvidence):
            continue
        request = lookup.get((event.run_id, payload.request_sequence))
        if (
            request is None
            or not isinstance(request.payload, ControlEvidence)
            or request.payload.reason != "history_request"
            or request.sequence >= event.sequence
        ):
            history_verified = False
    if not history_verified:
        missing.append(
            "Exact phase-local history request/availability provenance missing"
        )
    failures = {
        "recording_failure",
        "recording_interruption",
        "missing_sequence",
        "stale_data",
        "queue_overflow",
    }
    capture_complete = not any(
        isinstance(event.payload, ControlEvidence)
        and event.payload.reason in failures
        and start <= (event.calibrated_received_utc or event.host_observed_utc) <= end
        for event in evidence
    )
    if not capture_complete:
        missing.append("Recording continuity failed within the frozen period")
    windows_finalized = all(
        row.get("status") != "pending"
        for kind in ("followups", "late_returns")
        for row in records.get(kind, [])
    )
    # Missing/incomplete future windows cannot certify the period merely because
    # a report renderer converted pending rows to a labelled incomplete state.
    windows_finalized = windows_finalized and all(
        row.get("status") == "complete"
        for kind in ("followups", "late_returns")
        for row in records.get(kind, [])
        if row.get("phase_eligible", True)
    )
    if not windows_finalized:
        missing.append("Required outcome windows are incomplete")
    period_finalized = (
        now >= end
        and through
        and session_complete
        and capture_complete
        and windows_finalized
    )
    return JSON.validate_python(
        {
            "period_finalized": period_finalized,
            "common_dates": common_dates,
            "expected_dates": sorted(expected),
            "missing": missing,
            "session_complete": session_complete,
            "history_verified": history_verified,
            "timing_verified": timing_verified,
            "parity_verified": False,
            "capture_complete": capture_complete,
            "source_watermarks": {
                market: time.isoformat() for market, time in watermarks.items()
            },
            "frozen_end": end.isoformat(),
            "windows_finalized": windows_finalized,
        }
    )


def evaluate(
    definition: Manifest,
    evidence: tuple[Envelope, ...],
    capture_states: dict[str, str] | None = None,
) -> tuple[dict[str, JsonValue], dict[str, list[Row]]]:
    if any(
        isinstance(event.payload, ControlEvidence)
        and event.payload.reason == "operating_independent_v2"
        for event in evidence
    ):
        raise ValueError(
            "Independent-v2 evidence cannot be mixed into the historical paired-rule experiment. Create a separate independent-market experiment."
        )
    captured = snapshot(definition, evidence, capture_states)
    phase = "validation" if definition.label == "validation" else "development"
    records, parity = _phase(definition, evidence, phase)
    finalized = finalization(definition, evidence, records)
    if capture_states is not None and any(
        capture_states.get(event.run_id) != "closed" for event in evidence
    ):
        finalized["period_finalized"] = False
        finalized["capture_complete"] = False
        missing = finalized.get("missing")
        finalized["missing"] = [
            *(missing if isinstance(missing, list) else []),
            "Recording run is open or incomplete; committed prefix cannot certify final evidence",
        ]
    finalized["parity_verified"] = parity == "verified"
    # US2 exposes descriptive results. Formal uncertainty is added separately;
    # placeholders are unknown, never numerical zeros.
    for group in records["groups"]:
        measurements = [
            row
            for row in records["followups"]
            if row["market"] == group["market"] and row["variant"] == group["variant"]
        ]
        complete = [row for row in measurements if row["status"] == "complete"]
        group["complete_measurements"] = len(complete)
        group["incomplete_measurements"] = len(measurements) - len(complete)
        primary = [row for row in complete if row["eligible_primary_match"]]
        points = [float(str(row["points"])) for row in primary]
        group["observations"] = len(primary)
        group["returns"] = sum(
            row.get("return_time") is not None
            for row in records["attempts"]
            if row["market"] == group["market"] and row["variant"] == group["variant"]
        )
        group["late_returns"] = sum(
            row["first_return"] is not None
            for row in records["late_returns"]
            if row["market"] == group["market"] and row["variant"] == group["variant"]
        )
        group["median_points"] = (
            quantiles(points, n=2)[0]
            if len(points) > 1
            else points[0]
            if points
            else None
        )
        group["normal_mean"] = (
            mean(points) if points and definition.costs is not None else None
        )
        group["frequency_per_hour"] = (
            float(str(group["starts"])) / float(str(group["target_hours"]))
            if group["target_hours"]
            else None
        )
    claims: list[Row] = [
        {
            "claim": f"mean:{market}:{variant}",
            "lower": None,
            "reason": "Formal validation not evaluated",
        }
        for market in MARKETS
        for variant in VARIANTS
    ]
    comparisons: list[Row] = [
        {
            "market": market,
            "variant": variant,
            "baseline": "confirmed-90",
            "decision": "insufficient",
            "reason": "Prospective finalized evidence, verified sessions and costs required",
            "improvement": None,
            "improvement_lower": None,
        }
        for market in MARKETS
        for variant in VARIANTS
        if variant != "confirmed-90"
    ]
    claims.extend(
        {
            "claim": f"contrast:{row['market']}:{row['variant']}",
            "lower": None,
            "reason": "Formal validation not evaluated",
        }
        for row in comparisons
    )
    observations = []
    if definition.costs is not None:
        for row in records["followups"]:
            if (
                row["status"] != "complete"
                or not row["eligible_primary_match"]
                or not row.get("phase_eligible")
            ):
                continue
            market, variant = row["market"], row["variant"]
            if (
                not isinstance(market, str)
                or not isinstance(variant, str)
                or market not in MARKETS
                or variant not in VARIANTS
            ):
                raise ValueError("Invalid primary group")
            observation = Observation(
                market=market,
                variant=variant,
                date=time_of(row, "return_time").astimezone(UTC).date().isoformat(),
                points=float(str(row["points"])),
                entry_spread=float(str(row["entry_spread"])),
                exit_spread=float(str(row["exit_spread"])),
                nonspread_cost=definition.costs.total(),
            )
            row.update(
                normal=observation.normal,
                doubled=observation.doubled,
                nonspread_cost=observation.nonspread_cost,
                cost_provenance=definition.costs.provenance,
            )
            observations.append(observation)
    common = finalized.get("common_dates")
    dates = (
        sorted({item.date for item in observations})
        if phase == "development"
        else [day for day in common if isinstance(day, str)]
        if isinstance(common, list)
        else []
    )
    gates = GateEvidence(
        period_finalized=finalized.get("period_finalized") is True,
        session_complete=finalized.get("session_complete") is True,
        costs_known=definition.costs is not None,
        timing_verified=finalized.get("timing_verified") is True
        and definition.costs is not None
        and definition.costs.timely_execution,
        history_verified=finalized.get("history_verified") is True,
        parity_verified=parity == "verified",
        phase_validation=phase == "validation",
    )
    statistics, comparisons, claims = assess(observations, dates, gates)
    for group, stats in zip(records["groups"], statistics, strict=True):
        group.update(stats)
        if group["variant"] == "confirmed-90":
            group.update(
                decision="insufficient",
                reason="Operating-rule reference; prospective finalized evidence and complete costs required"
                if not gates.period_finalized
                else "Reference group; alternatives are evaluated against it",
            )
        if definition.costs is None:
            group.update(normal_mean=None, doubled_mean=None, mean_lower=None)
    report = JSON.validate_python(
        {
            "schema_version": 1,
            "rule_version": RULE_VERSION,
            "report_id": digest(
                {
                    "definition": definition.identity(),
                    "snapshot": captured.identity(),
                    "rules": RULE_VERSION,
                }
            ),
            "experiment_id": definition.identity(),
            "phase_bounds": {
                "development_start": definition.development_start.isoformat(),
                "development_end": definition.development_end.isoformat(),
                "validation_start": definition.validation_start.isoformat()
                if definition.validation_start
                else None,
                "validation_end": definition.validation_end.isoformat()
                if definition.validation_end
                else None,
            },
            "snapshot": {
                **captured.model_dump(mode="json"),
                "snapshot_id": captured.identity(),
            },
            "status": "completed",
            "label": definition.label,
            "phase": phase,
            "finalization": finalized,
            "costs": definition.costs.model_dump(mode="json")
            if definition.costs
            else None,
            "method": {
                "primary_horizon": 180,
                "secondary_horizons": [60, 300],
                "replicates": 50000,
                "seed": 204,
                "block_dates": 5,
                "lower_order": 179,
                "claims": 14,
                "assumptions": "Approximate uncertainty under dependence/stability assumptions; further demo research only.",
            },
            "limitations": [
                "Synthetic evidence is invented; it is not a market backtest."
                if definition.label == "synthetic"
                else "Research observations; delayed prices cannot prove timely execution.",
                "No orders, position sizes or account profits are simulated.",
                "Legacy provenance is not reconstructed.",
                *(
                    [
                        "PARTIAL LEGACY IMPORT: only source mapping/counts/hash retained; exact causal replay and historical checks unavailable."
                    ]
                    if any(event.source_id == "legacy-unverified" for event in evidence)
                    else []
                ),
            ],
            "parity": parity,
            "markets": list(MARKETS),
            "groups": records["groups"],
            "comparisons": comparisons,
            "claims": claims,
        }
    )
    return report, records


def render(report: dict[str, JsonValue], records: dict[str, list[Row]]) -> str:
    parts = [
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Gold and oil research evaluation</title><style>body{font:17px/1.6 system-ui;background:#0d1521;color:#edf3fc;max-width:1100px;margin:32px auto;padding:16px}table{border-collapse:collapse}td,th{padding:12px;text-align:left;border:1px solid #53647a}.scroll{overflow:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><h1>Gold and oil research evaluation</h1>",
        f"<p>{html.escape(str(report['label']))} · Research only. No trade was placed.</p>",
        "<p>More matches do not establish improvement. Read the evidence status and missing data below.</p>",
    ]
    for kind, rows in records.items():
        parts.append(
            f"<h2>{html.escape(kind.replace('_', ' ').title())}</h2><div class=scroll><table>"
        )
        if rows:
            columns = list(dict.fromkeys(key for row in rows for key in row))
            parts.append(
                "<tr>"
                + "".join(
                    f"<th>{html.escape(key.replace('_', ' '))}</th>" for key in columns
                )
                + "</tr>"
            )
            parts.extend(
                "<tr>"
                + "".join(
                    f"<td>{html.escape(str(row.get(key) if row.get(key) is not None else 'Unavailable'))}</td>"
                    for key in columns
                )
                + "</tr>"
                for row in rows
            )
        parts.append("</table></div>")
    parts.append(
        "<h2>Evidence identity and limits</h2><pre>"
        + html.escape(json.dumps(report, indent=2))
        + "</pre>"
    )
    return "".join(parts)


def export(
    output: Path, report: dict[str, JsonValue], records: dict[str, list[Row]]
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    files = {
        "report.json": json.dumps(report, indent=2, sort_keys=True, allow_nan=False),
        "report.html": render(report, records),
    }
    for kind, rows in records.items():
        stream = io.StringIO(newline="")
        columns = list(dict.fromkeys(key for row in rows for key in row))
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        files[kind + ".csv"] = stream.getvalue()
    marker = output / "complete.json"
    marker.unlink(missing_ok=True)
    for name, body in files.items():
        temporary = output / (name + ".tmp")
        temporary.write_text(body, encoding="utf-8")
        temporary.replace(output / name)
    temporary = output / "complete.json.tmp"
    temporary.write_text(
        json.dumps(
            {
                "report_id": report["report_id"],
                "files": {name: digest(body) for name, body in files.items()},
            }
        ),
        encoding="utf-8",
    )
    temporary.replace(marker)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--store", type=Path, default=None)
    try:
        args = parser.parse_args(argv)
        definition = Manifest.model_validate_json(
            args.manifest.read_text(encoding="utf-8")
        )
        evidence, capture_states = read_evidence(args.evidence, definition)
        report, records = evaluate(definition, evidence, capture_states)
    except SystemExit as exc:
        return int(exc.code or 0)
    except ValidationError, ValueError:
        print(
            "Invalid manifest/evidence: verify identities, phase bounds and provenance.",
            file=sys.stderr,
        )
        return 2
    except OSError, sqlite3.Error:
        print(
            "Evidence input could not be read; no report was published.",
            file=sys.stderr,
        )
        return 1
    try:
        export(args.output, report, records)
        EvaluationStore(args.store or store_path()).register(report, records)
    except OSError, sqlite3.Error, ValueError, RuntimeError:
        print(
            "Export or registration failed; previous registered report is retained.",
            file=sys.stderr,
        )
        return 1
    print(
        f"Research report registered: {report['report_id']} ({definition.label}; no operating rules changed)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

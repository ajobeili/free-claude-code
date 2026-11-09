"""Private SQLite evidence, atomic publication and bounded commit-driven work."""

import asyncio
import csv
import io
import json
import sqlite3
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import JsonValue, TypeAdapter

from tools.pattern_evaluation import Evaluator
from tools.pattern_evaluation_events import (
    ControlEvidence,
    Envelope,
    HistoryEvidence,
    Market,
    OperatingDecision,
    QuoteEvidence,
    Row,
    digest,
)

JSON = TypeAdapter(dict[str, JsonValue])
KINDS = ("coverage", "stages", "attempts", "followups", "late_returns", "groups")
DIAGNOSTIC_KINDS = ("coverage", "stages", "attempts")
COMMON_COLUMNS = {"run_id", "market", "variant", "phase", "sequence", "phase_eligible"}
RECORD_COLUMNS = {
    "coverage": COMMON_COLUMNS
    | {"start", "end", "paired", "seconds", "sessions_verified"},
    "stages": COMMON_COLUMNS
    | {"reason", "state", "unit", "count", "sequence", "stage", "primary_reason"},
    "attempts": COMMON_COLUMNS
    | {
        "attempt_id",
        "start_sequence",
        "started",
        "observed_time",
        "deadline",
        "limit_seconds",
        "sign",
        "range_high",
        "range_low",
        "history_hash",
        "breakout_price",
        "other_start",
        "outcome",
        "reason",
        "return_time",
        "return_sequence",
        "return_bid",
        "return_offer",
        "return_spread",
        "ended",
        "decision_sequence",
    },
    "followups": COMMON_COLUMNS
    | {
        "attempt_id",
        "return_time",
        "horizon_seconds",
        "due",
        "direction",
        "entry",
        "entry_spread",
        "exit",
        "exit_spread",
        "points",
        "best",
        "worst",
        "status",
        "reason",
        "eligible_primary_match",
        "last_time",
        "exit_time",
        "exit_sequence",
        "normal",
        "doubled",
        "nonspread_cost",
        "cost_provenance",
        "phase_eligible",
    },
    "late_returns": COMMON_COLUMNS
    | {
        "attempt_id",
        "deadline",
        "started",
        "end",
        "range_high",
        "range_low",
        "status",
        "reason",
        "first_return",
        "elapsed_seconds",
        "return_sequence",
    },
    "groups": COMMON_COLUMNS
    | {
        "starts",
        "matches",
        "expired",
        "context_failed",
        "complete_measurements",
        "incomplete_measurements",
        "target_hours",
        "paired_hours",
        "normal_mean",
        "doubled_mean",
        "decision",
        "reason",
        "returns",
        "late_returns",
        "observations",
        "return_dates",
        "mean_lower",
        "improvement",
        "improvement_lower",
        "primary_horizon",
        "frequency_per_hour",
        "median_points",
        "p25_points",
        "p75_points",
    },
}
REPORT_COLUMNS = {
    "schema_version",
    "rule_version",
    "report_id",
    "experiment_id",
    "phase_bounds",
    "snapshot",
    "status",
    "label",
    "limitations",
    "parity",
    "markets",
    "groups",
    "comparisons",
    "claims",
    "method",
    "decision",
    "source_id",
    "finalization",
    "evidence_decisions",
    "costs",
    "phase",
    "definition",
}


class RetiredRevision(ValueError):
    """Refresh the summary before selecting an evicted diagnostic revision."""


class EvaluationStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                body TEXT NOT NULL, UNIQUE(run_id, sequence));
            CREATE TABLE IF NOT EXISTS reports (
                ordinal INTEGER PRIMARY KEY, report_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS results (
                report_id TEXT NOT NULL, kind TEXT NOT NULL, ordinal INTEGER NOT NULL,
                body TEXT NOT NULL, PRIMARY KEY(report_id, kind, ordinal));
            CREATE TABLE IF NOT EXISTS report_hashes (
                report_id TEXT PRIMARY KEY, records_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS capture_runs (
                run_id TEXT PRIMARY KEY, status TEXT NOT NULL, dropped INTEGER NOT NULL);
        """)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def append(self, events: Sequence[Envelope]) -> None:
        with self._connect() as connection:
            for event in events:
                previous = connection.execute(
                    "SELECT max(sequence) FROM evidence WHERE run_id=?", (event.run_id,)
                ).fetchone()[0]
                if event.sequence != (previous or 0) + 1:
                    raise ValueError("Duplicate or missing evidence sequence")
                connection.execute(
                    "INSERT INTO evidence(run_id,sequence,body) VALUES (?,?,?)",
                    (event.run_id, event.sequence, event.model_dump_json()),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO capture_runs VALUES (?,'open',0)",
                    (event.run_id,),
                )

    def mark_run(self, run_id: str, status: str, dropped: int) -> None:
        if status not in ("open", "closed", "incomplete"):
            raise ValueError("Invalid capture status")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO capture_runs VALUES (?,?,?) ON CONFLICT(run_id) DO UPDATE SET status=CASE WHEN capture_runs.status='incomplete' THEN 'incomplete' ELSE excluded.status END,dropped=max(capture_runs.dropped,excluded.dropped)",
                (run_id, status, dropped),
            )

    def run_status(self) -> dict[str, str]:
        with self._connect() as connection:
            return dict(connection.execute("SELECT run_id,status FROM capture_runs"))

    def evidence(
        self, after: int = 0, limit: int | None = None
    ) -> tuple[Envelope, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT body FROM evidence WHERE id>? ORDER BY id LIMIT ?",
                (after, limit if limit is not None else -1),
            ).fetchall()
        return tuple(Envelope.model_validate_json(row[0]) for row in rows)

    def register(
        self, report: dict[str, JsonValue], records: dict[str, list[Row]]
    ) -> None:
        report_id = report.get("report_id")
        if (
            report.get("status") != "completed"
            or not isinstance(report_id, str)
            or len(report_id) != 64
            or set(records) - set(KINDS)
            or set(report) - REPORT_COLUMNS
        ):
            raise ValueError("Only completed allowlisted reports can be registered")
        body = json.dumps(report, sort_keys=True, allow_nan=False)
        for kind, rows in records.items():
            if any(set(row) - RECORD_COLUMNS[kind] for row in rows):
                raise ValueError("Unsupported or private record fields")
        records_hash = digest(records)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT body FROM reports WHERE report_id=?", (report_id,)
            ).fetchone()
            if existing:
                previous_hash = connection.execute(
                    "SELECT records_hash FROM report_hashes WHERE report_id=?",
                    (report_id,),
                ).fetchone()
                if (
                    existing[0] != body
                    or previous_hash is None
                    or previous_hash[0] != records_hash
                ):
                    raise ValueError("Immutable report identity conflict")
                return
            for kind, rows in records.items():
                for ordinal, row in enumerate(rows):
                    connection.execute(
                        "INSERT INTO results VALUES (?,?,?,?)",
                        (report_id, kind, ordinal, json.dumps(row, allow_nan=False)),
                    )
            connection.execute(
                "INSERT INTO reports(report_id,body) VALUES (?,?)", (report_id, body)
            )
            connection.execute(
                "INSERT INTO report_hashes VALUES (?,?)", (report_id, records_hash)
            )

    def latest(self) -> dict[str, JsonValue] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT body FROM reports ORDER BY ordinal DESC LIMIT 1"
            ).fetchone()
        return JSON.validate_json(row[0]) if row else None

    def records(
        self,
        report_id: str,
        kind: str,
        filters: dict[str, str],
        offset: int,
        limit: int,
    ) -> dict[str, JsonValue]:
        if kind not in KINDS or offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid list selection")
        with self._connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM reports WHERE report_id=?", (report_id,)
            ).fetchone():
                raise KeyError(report_id)
            # Each allowed filter names a scalar field; values are always bound.
            clauses = ["report_id=?", "kind=?"]
            parameters: list[str | int] = [report_id, kind]
            for key, value in filters.items():
                if key not in ("market", "variant", "phase"):
                    raise ValueError("Invalid filter")
                clauses.append(f"json_extract(body, '$.{key}')=?")
                parameters.append(value)
            query = (
                "SELECT body FROM results WHERE "
                + " AND ".join(clauses)
                + " ORDER BY ordinal LIMIT ? OFFSET ?"
            )
            rows = [
                JSON.validate_json(row[0])
                for row in connection.execute(query, [*parameters, limit + 1, offset])
            ]
        return page(rows[:limit], offset, len(rows) > limit, report_id=report_id)


def page(
    rows: list[dict[str, JsonValue]], offset: int, more: bool, **identity: str | int
) -> dict[str, JsonValue]:
    columns = list(dict.fromkeys(key for row in rows for key in row))
    return JSON.validate_python(
        {
            **identity,
            "columns": columns,
            "rows": [[row.get(key) for key in columns] for row in rows],
            "offset": offset,
            "has_more": more,
            "limitations": [],
        }
    )


def csv_page(result: dict[str, JsonValue]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    columns, rows = result.get("columns"), result.get("rows")
    if isinstance(columns, list):
        writer.writerow(columns)
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, list):
                writer.writerow(row)
    return output.getvalue()


class EvaluationRecorder:
    def __init__(self, path: Path, *, capacity: int = 10000):
        self.store = EvaluationStore(path)
        self._queue: asyncio.Queue[Envelope] = asyncio.Queue(capacity)
        self._run_id = uuid.uuid4().hex
        self._sequence = 0
        self._committed = 0
        self._dropped = 0
        self._error = ""
        self._clock_offset: float | None = None
        self._calibration_id: str | None = None
        self._running = False
        self._writer: asyncio.Task[None] | None = None
        self._consumer: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._changed = asyncio.Condition()
        self._version = 0
        self._cursor = 0
        self._revision = 0
        self._diagnostic_error = ""
        self._processed_time: str | None = None
        self._reducer = Evaluator(("confirmed-90",), retention=1000)
        self._revisions: dict[int, dict[str, list[Row]]] = {}
        self._diagnostics: dict[str, JsonValue] = {
            "status": "collecting",
            "revision": 0,
            "processed_through_sequence": 0,
            "lag": 0,
            "limitations": [
                "Legacy evidence lacks exact receipt order/history; not reconstructed."
            ],
        }

    async def start(self) -> None:
        if self._running:
            return
        try:
            await asyncio.to_thread(self.store.evidence, 0, 0)
        except OSError, sqlite3.Error, ValueError:
            self._error = (
                "Research storage unavailable; operating alerts are unaffected."
            )
            return
        self._running = True
        self._writer = asyncio.create_task(self._write())
        self._consumer = asyncio.create_task(self._consume())
        self._wake.set()

    def sequence(self) -> int:
        return self._sequence

    async def updates(self) -> AsyncIterator[int]:
        version = -1
        while True:
            async with self._changed:
                await self._changed.wait_for(
                    lambda expected=version: self._version != expected
                )
                version = self._version
            yield version

    async def _publish(self) -> None:
        async with self._changed:
            self._version += 1
            self._changed.notify_all()

    def enqueue(
        self,
        kind: str,
        payload: QuoteEvidence | HistoryEvidence | ControlEvidence | OperatingDecision,
        *,
        epic: Market | None = None,
        source_time: datetime | None = None,
        received: datetime | None = None,
        calibration_id: str | None = None,
    ) -> None:
        if not self._running:
            return
        if self._error:
            self._dropped += 1
            return
        self._sequence += 1
        host_time = datetime.now(UTC)
        if (
            isinstance(payload, ControlEvidence)
            and payload.clock_offset_seconds is not None
        ):
            self._clock_offset = payload.clock_offset_seconds
            self._calibration_id = calibration_id
        if received is None and self._clock_offset is not None:
            received = host_time - timedelta(seconds=self._clock_offset)
        if calibration_id is None:
            calibration_id = self._calibration_id
        try:
            event = Envelope.model_validate(
                {
                    "run_id": self._run_id,
                    "sequence": self._sequence,
                    "kind": kind,
                    "epic": epic,
                    "source_time": source_time,
                    "host_observed_utc": host_time,
                    "calibrated_received_utc": received,
                    "calibration_id": calibration_id,
                    "payload": payload,
                }
            )
        except ValueError, TypeError:
            self._error = "Research evidence rejected; recording is incomplete."
            self._dropped += 1
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
            self._error = "Research queue overflow; evidence is incomplete."

    def control(self, reason: str, connected: bool | None = None) -> None:
        self.enqueue("control", ControlEvidence(reason=reason, connected=connected))

    def snapshot(self) -> dict[str, JsonValue]:
        return {
            "enabled": True,
            "running": self._running,
            "error": self._error,
            "dropped": self._dropped,
            "run_id": self._run_id,
            "last_committed_sequence": self._committed,
        }

    def diagnostics(self) -> dict[str, JsonValue]:
        return {
            **self._diagnostics,
            "capture_watermark": self._committed,
            "error": self._diagnostic_error or self._error,
            "status": "error"
            if self._diagnostic_error or self._error
            else "stale"
            if not self._running
            else self._diagnostics["status"],
        }

    async def _write(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while len(batch) < 250 and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                await asyncio.to_thread(self.store.append, batch)
                self._committed = batch[-1].sequence
                if self._error:
                    await asyncio.to_thread(
                        self.store.mark_run, self._run_id, "incomplete", self._dropped
                    )
                self._wake.set()
                await self._publish()
            except OSError, sqlite3.Error, ValueError:
                self._error = "Research recording failed; evidence is incomplete."
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _consume(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                while True:
                    events = await asyncio.to_thread(
                        self.store.evidence, self._cursor, 250
                    )
                    if not events:
                        break
                    records, summary = await asyncio.to_thread(
                        self._reduce_batch, events
                    )
                    self._cursor += len(events)
                    self._revision += 1
                    self._revisions[self._revision] = {
                        kind: records[kind] for kind in DIAGNOSTIC_KINDS
                    }
                    while len(self._revisions) > 2:
                        del self._revisions[min(self._revisions)]
                    processed = summary.get("processed_through_sequence")
                    same_run = summary.get("run_id") == self._run_id
                    lag = (
                        max(0, self._committed - int(processed))
                        if same_run and isinstance(processed, int)
                        else self._committed
                    )
                    limitations = summary.get("limitations")
                    self._diagnostics = {
                        **summary,
                        "revision": self._revision,
                        "processed_cursor": self._cursor,
                        "status": "current" if lag == 0 else "collecting",
                        "lag": lag,
                        "retention": "Latest 1000 detail rows; refresh pins a new revision.",
                        "limitations": [
                            *(limitations if isinstance(limitations, list) else []),
                            "Session calendar unverified; coverage is observed research time only.",
                        ],
                    }
                    await self._publish()
                    await asyncio.sleep(0)
            except (
                OSError,
                sqlite3.Error,
                ValueError,
                RuntimeError,
                TypeError,
                KeyError,
                ArithmeticError,
            ):
                self._diagnostic_error = "Research diagnostics failed; restart to recover from committed evidence."
                await self._publish()

    def _reduce_batch(
        self, events: tuple[Envelope, ...]
    ) -> tuple[dict[str, list[Row]], dict[str, JsonValue]]:
        for event in events:
            self._reducer.process(event)
        return self._reducer.records(), self._reducer.summary()

    async def drain(self) -> None:
        await self._queue.join()
        if self._consumer is not None and not self._diagnostic_error:
            self._wake.set()
            while (
                self._committed
                and self._diagnostics.get("processed_through_sequence")
                != self._committed
                and not self._diagnostic_error
            ):
                await asyncio.sleep(0.001)

    async def close(self) -> None:
        try:
            async with asyncio.timeout(5):
                await self.drain()
        except TimeoutError:
            self._error = "Research shutdown incomplete."
        self._running = False
        try:
            await asyncio.to_thread(
                self.store.mark_run,
                self._run_id,
                "incomplete" if self._error else "closed",
                self._dropped,
            )
        except OSError, sqlite3.Error, ValueError:
            self._error = "Research shutdown incomplete."
        for task in (self._writer, self._consumer):
            if task:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._writer, self._consumer) if task),
            return_exceptions=True,
        )

    def diagnostic_records(
        self, revision: int, kind: str, filters: dict[str, str], offset: int, limit: int
    ) -> dict[str, JsonValue]:
        if kind not in DIAGNOSTIC_KINDS or offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid diagnostic list")
        if revision not in self._revisions:
            raise RetiredRevision("Diagnostic revision retired; refresh the summary.")
        rows = [
            JSON.validate_python(row)
            for row in self._revisions[revision][kind]
            if all(row.get(key) == value for key, value in filters.items())
        ]
        return page(
            rows[offset : offset + limit],
            offset,
            len(rows) > offset + limit,
            diagnostic_revision=revision,
        )

    def diagnostic_export(
        self, revision: int, kind: str, filters: dict[str, str]
    ) -> tuple[Row, ...]:
        """Pin immutable retained rows before starting an HTTP stream."""
        if kind not in DIAGNOSTIC_KINDS or set(filters) - {
            "market",
            "variant",
            "phase",
        }:
            raise ValueError("Invalid diagnostic export")
        if revision not in self._revisions:
            raise RetiredRevision("Diagnostic revision retired; refresh the summary.")
        return tuple(
            row
            for row in self._revisions[revision][kind]
            if all(row.get(key) == value for key, value in filters.items())
        )

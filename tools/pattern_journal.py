"""Persistent quote evidence and spread-inclusive research markouts, without orders."""

import asyncio
import csv
import io
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from tools.pattern_events import (
    AttemptRecord,
    DecisionRecord,
    QuoteRecord,
    RangeRecord,
    ResearchRecord,
)

HORIZONS = (60, 180, 300)
MAX_GAP = 5

JOURNAL_QUERIES = {
    "quotes": "SELECT * FROM quotes ORDER BY id",
    "attempts": "SELECT * FROM attempts ORDER BY rowid",
    "outcomes": """SELECT a.epic,
        CASE WHEN a.sign=1 THEN 'DOWN' ELSE 'UP' END AS reversal_direction,
        a.started AS breakout_time,a.ended AS reentry_time,
        a.outcome AS pattern_result,a.run_id,o.* FROM outcomes o
        JOIN attempts a ON a.id=o.attempt_id ORDER BY o.rowid""",
    "runs": "SELECT * FROM runs ORDER BY rowid",
    "ranges": "SELECT * FROM reference_ranges ORDER BY id",
    "gaps": "SELECT * FROM gaps ORDER BY id",
}


class JournalPage(TypedDict):
    columns: list[str]
    rows: list[list[str | int | float | None]]
    offset: int
    has_more: bool


class AttemptView(TypedDict):
    epic: str
    started: str
    outcome: str
    reason: str
    rule_version: str


class MarkoutView(TypedDict):
    epic: str
    seconds: int
    observations: int
    qualified_observations: int
    mean_change_points: float | None
    qualified_mean_change_points: float | None


class JournalSummary(TypedDict):
    quotes: int
    attempts: int
    reentries: int
    qualified: int
    complete_outcomes: int
    incomplete_outcomes: int
    pending_outcomes: int
    recent: list[AttemptView]
    markouts: list[MarkoutView]


class JournalStatus(JournalSummary):
    running: bool
    error: str
    dropped: int
    pending_writes: int


def empty_summary() -> JournalSummary:
    return {
        "quotes": 0,
        "attempts": 0,
        "reentries": 0,
        "qualified": 0,
        "complete_outcomes": 0,
        "incomplete_outcomes": 0,
        "pending_outcomes": 0,
        "recent": [],
        "markouts": [],
    }


@dataclass
class Followup:
    key: str
    epic: str
    sign: int
    entry: float
    started: datetime
    last: datetime
    best: float = 0
    worst: float = 0
    pending: set[int] = field(default_factory=lambda: set(HORIZONS))


class JournalStore:
    def __init__(self, path: Path):
        self._path = path
        self._run = ""
        self._followups: dict[str, Followup] = {}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=2, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def start(self, manifest: dict[str, str | int | float | bool]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._run = uuid4().hex
        self._followups.clear()
        with closing(self._connect()) as c, c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, started TEXT, manifest TEXT,
                    quotes INTEGER NOT NULL DEFAULT 0,
                    recording_error TEXT NOT NULL DEFAULT '', dropped INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS quotes (
                    id INTEGER PRIMARY KEY, run_id TEXT, epic TEXT,
                    event_time TEXT, received_time TEXT, bid REAL, offer REAL, quality TEXT);
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, run_id TEXT, epic TEXT, started TEXT,
                    sign INTEGER, range_high REAL, range_low REAL, breakout_price REAL,
                    other_start REAL, ended TEXT, outcome TEXT, reason TEXT,
                    bid REAL, offer REAL, other_end REAL);
                CREATE TABLE IF NOT EXISTS reference_ranges (
                    id INTEGER PRIMARY KEY, run_id TEXT, epic TEXT,
                    event_time TEXT, high REAL, low REAL);
                CREATE TABLE IF NOT EXISTS gaps (
                    id INTEGER PRIMARY KEY, run_id TEXT, observed_host_utc TEXT, reason TEXT);
                CREATE TABLE IF NOT EXISTS outcomes (
                    attempt_id TEXT, horizon INTEGER, entry REAL, result_time TEXT,
                    exit_price REAL, change_points REAL, mfe_points REAL, mae_points REAL,
                    status TEXT, reason TEXT, PRIMARY KEY(attempt_id,horizon));
                CREATE INDEX IF NOT EXISTS quote_event ON quotes(run_id,epic,event_time);
                CREATE INDEX IF NOT EXISTS attempt_run ON attempts(run_id,outcome);
            """)
            c.execute(
                "UPDATE attempts SET outcome='interrupted',reason='server_restart' WHERE outcome='open'"
            )
            c.execute(
                "UPDATE outcomes SET status='incomplete',reason='server_restart' WHERE status='pending'"
            )
            c.execute(
                "INSERT INTO runs(id,started,manifest) VALUES(?,?,?)",
                (
                    self._run,
                    datetime.now(UTC).isoformat(),
                    json.dumps(manifest, sort_keys=True),
                ),
            )

    def _key(self, epic: str, started: datetime) -> str:
        return f"{self._run}:{epic}:{started.isoformat()}"

    def _interrupt(self, c: sqlite3.Connection, reason: str) -> None:
        c.execute(
            "UPDATE attempts SET outcome='interrupted',reason=? WHERE run_id=? AND outcome='open'",
            (reason, self._run),
        )
        for key in self._followups:
            c.execute(
                "UPDATE outcomes SET status='incomplete',reason=? WHERE attempt_id=? AND status='pending'",
                (reason, key),
            )
        self._followups.clear()

    def _quote(self, c: sqlite3.Connection, event: QuoteRecord) -> None:
        c.execute(
            "INSERT INTO quotes(run_id,epic,event_time,received_time,bid,offer,quality) VALUES(?,?,?,?,?,?,?)",
            (
                self._run,
                event.epic,
                event.time.isoformat(),
                event.received.isoformat(),
                event.bid,
                event.offer,
                event.quality,
            ),
        )
        c.execute("UPDATE runs SET quotes=quotes+1 WHERE id=?", (self._run,))
        if event.quality not in ("timely", "delayed_research"):
            return
        for key, follow in list(self._followups.items()):
            if follow.epic != event.epic or event.time <= follow.last:
                continue
            if (event.time - follow.last).total_seconds() > MAX_GAP:
                c.execute(
                    "UPDATE outcomes SET status='incomplete',reason='quote_gap' WHERE attempt_id=? AND status='pending'",
                    (key,),
                )
                del self._followups[key]
                continue
            exit_price = event.bid if follow.sign == 1 else event.offer
            change = follow.sign * (exit_price - follow.entry)
            follow.best = max(follow.best, change)
            follow.worst = min(follow.worst, change)
            follow.last = event.time
            elapsed = (event.time - follow.started).total_seconds()
            for horizon in sorted(follow.pending):
                if elapsed < horizon:
                    continue
                complete = elapsed <= horizon + MAX_GAP
                c.execute(
                    """UPDATE outcomes SET result_time=?,exit_price=?,change_points=?,
                    mfe_points=?,mae_points=?,status=?,reason=? WHERE attempt_id=? AND horizon=?""",
                    (
                        event.time.isoformat(),
                        exit_price if complete else None,
                        change if complete else None,
                        follow.best if complete else None,
                        follow.worst if complete else None,
                        "complete" if complete else "incomplete",
                        "observed_quote" if complete else "missing_horizon_quote",
                        key,
                        horizon,
                    ),
                )
                follow.pending.remove(horizon)
            if not follow.pending:
                del self._followups[key]

    def _decision(self, c: sqlite3.Connection, event: DecisionRecord) -> None:
        key = self._key(event.epic, event.started)
        row = c.execute(
            "SELECT sign,outcome FROM attempts WHERE id=?", (key,)
        ).fetchone()
        if row is None or row["outcome"] != "open":
            return
        c.execute(
            "UPDATE attempts SET ended=?,outcome=?,reason=?,bid=?,offer=?,other_end=? WHERE id=?",
            (
                event.time.isoformat(),
                event.outcome,
                event.reason,
                event.bid,
                event.offer,
                event.other_end,
                key,
            ),
        )
        if (
            event.outcome not in ("qualified", "context_failed")
            or event.bid is None
            or event.offer is None
        ):
            return
        sign = -int(row["sign"])
        entry = event.offer if sign == 1 else event.bid
        self._followups[key] = Followup(
            key,
            event.epic,
            sign,
            entry,
            event.time,
            event.time,
            worst=-(event.offer - event.bid),
        )
        c.executemany(
            "INSERT INTO outcomes(attempt_id,horizon,entry,status,reason) VALUES(?,?,?,'pending','collecting')",
            [(key, horizon, entry) for horizon in HORIZONS],
        )

    def write(self, events: list[ResearchRecord]) -> None:
        with closing(self._connect()) as c, c:
            for event in events:
                if isinstance(event, QuoteRecord):
                    self._quote(c, event)
                elif isinstance(event, AttemptRecord):
                    c.execute(
                        """INSERT OR IGNORE INTO attempts(id,run_id,epic,started,sign,
                        range_high,range_low,breakout_price,other_start,outcome,reason)
                        VALUES(?,?,?,?,?,?,?,?,?,'open','watching_reentry')""",
                        (
                            self._key(event.epic, event.started),
                            self._run,
                            event.epic,
                            event.started.isoformat(),
                            event.sign,
                            event.range_high,
                            event.range_low,
                            event.breakout_price,
                            event.other_start,
                        ),
                    )
                elif isinstance(event, RangeRecord):
                    c.execute(
                        "INSERT INTO reference_ranges(run_id,epic,event_time,high,low) VALUES(?,?,?,?,?)",
                        (
                            self._run,
                            event.epic,
                            event.time.isoformat(),
                            event.high,
                            event.low,
                        ),
                    )
                elif isinstance(event, DecisionRecord):
                    self._decision(c, event)
                else:
                    c.execute(
                        "INSERT INTO gaps(run_id,observed_host_utc,reason) VALUES(?,?,?)",
                        (self._run, event.time.isoformat(), event.reason),
                    )
                    self._interrupt(c, event.reason)

    def fail(self, reason: str, dropped: int) -> None:
        with closing(self._connect()) as c, c:
            c.execute(
                "UPDATE runs SET recording_error=?,dropped=? WHERE id=?",
                (reason, dropped, self._run),
            )
            self._interrupt(c, "journal_incomplete")

    def summary(self) -> JournalSummary:
        result = empty_summary()
        with closing(self._connect()) as c:
            result["quotes"] = int(
                c.execute("SELECT COALESCE(SUM(quotes),0) FROM runs").fetchone()[0]
            )
            counts = {
                str(r[0]): int(r[1])
                for r in c.execute(
                    "SELECT outcome,COUNT(*) FROM attempts GROUP BY outcome"
                )
            }
            result["attempts"] = sum(counts.values())
            result["qualified"] = counts.get("qualified", 0)
            result["reentries"] = result["qualified"] + counts.get("context_failed", 0)
            counts = {
                str(r[0]): int(r[1])
                for r in c.execute(
                    "SELECT status,COUNT(*) FROM outcomes GROUP BY status"
                )
            }
            result["complete_outcomes"] = counts.get("complete", 0)
            result["incomplete_outcomes"] = counts.get("incomplete", 0)
            result["pending_outcomes"] = counts.get("pending", 0)
            result["recent"] = [
                {
                    "epic": str(r["epic"]),
                    "started": str(r["started"]),
                    "outcome": str(r["outcome"]),
                    "reason": str(r["reason"]),
                    "rule_version": str(r["rule_version"] or "quote-rejection-v1"),
                }
                for r in c.execute(
                    "SELECT a.epic,a.started,a.outcome,a.reason,json_extract(r.manifest,'$.rule_version') AS rule_version FROM attempts a JOIN runs r ON r.id=a.run_id ORDER BY a.rowid DESC LIMIT 8"
                )
            ]
            for r in c.execute(
                """SELECT a.epic,o.horizon,COUNT(*) AS n,
                    SUM(CASE WHEN a.outcome='qualified' THEN 1 ELSE 0 END) AS q,
                    AVG(o.change_points) AS mean_all,
                    AVG(CASE WHEN a.outcome='qualified' THEN o.change_points END) AS mean_q
                    FROM outcomes o JOIN attempts a ON a.id=o.attempt_id
                    JOIN runs r ON r.id=a.run_id
                    WHERE o.status='complete' AND r.recording_error=''
                    AND r.manifest=(SELECT manifest FROM runs WHERE id=?)
                    GROUP BY a.epic,o.horizon ORDER BY a.epic,o.horizon""",
                (self._run,),
            ):
                result["markouts"].append(
                    {
                        "epic": str(r["epic"]),
                        "seconds": int(r["horizon"]),
                        "observations": int(r["n"]),
                        "qualified_observations": int(r["q"]),
                        "mean_change_points": float(r["mean_all"])
                        if r["mean_all"] is not None
                        else None,
                        "qualified_mean_change_points": float(r["mean_q"])
                        if r["mean_q"] is not None
                        else None,
                    }
                )
        return result

    def export(self, kind: str) -> Iterator[str]:
        if kind not in JOURNAL_QUERIES or not self._path.exists():
            return
        with closing(self._connect()) as c:
            cursor = c.execute(JOURNAL_QUERIES[kind])
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow([item[0] for item in cursor.description or []])
            yield buffer.getvalue()
            while rows := cursor.fetchmany(500):
                buffer.seek(0)
                buffer.truncate(0)
                writer.writerows(rows)
                yield buffer.getvalue()

    def records(self, kind: str, offset: int = 0, limit: int = 25) -> JournalPage:
        if kind not in JOURNAL_QUERIES or offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Invalid journal page")
        result: JournalPage = {
            "columns": [],
            "rows": [],
            "offset": offset,
            "has_more": False,
        }
        if not self._path.exists():
            return result
        with closing(self._connect()) as c:
            cursor = c.execute(
                JOURNAL_QUERIES[kind] + " LIMIT ? OFFSET ?", (limit + 1, offset)
            )
            rows = cursor.fetchall()
            result["columns"] = [item[0] for item in cursor.description or []]
            result["rows"] = [list(row) for row in rows[:limit]]
            result["has_more"] = len(rows) > limit
        return result


class JournalRecorder:
    def __init__(self, path: Path):
        self._store = JournalStore(path)
        self._queue: asyncio.Queue[ResearchRecord] = asyncio.Queue(maxsize=10000)
        self._summary = empty_summary()
        self._error = ""
        self._started = False
        self._dropped = 0

    async def start(self, manifest: dict[str, str | int | float | bool]) -> None:
        try:
            await asyncio.to_thread(self._store.start, manifest)
            self._summary = await asyncio.to_thread(self._store.summary)
            self._started = True
        except sqlite3.Error, OSError, ValueError:
            self._error = "Journal storage unavailable; evidence is incomplete."

    def enqueue(self, event: ResearchRecord) -> None:
        if not self._started or self._error:
            self._dropped += 1
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1
            self._error = "Journal queue overflow; evidence is incomplete."

    def snapshot(self) -> JournalStatus:
        return {
            **self._summary,
            "running": self._started and not self._error,
            "error": self._error,
            "dropped": self._dropped,
            "pending_writes": self._queue.qsize(),
        }

    async def run(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while len(batch) < 250 and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                if not self._error:
                    await asyncio.to_thread(self._store.write, batch)
                    self._summary = await asyncio.to_thread(self._store.summary)
                else:
                    self._dropped += len(batch)
                    await asyncio.to_thread(
                        self._store.fail, self._error, self._dropped
                    )
                    self._summary = await asyncio.to_thread(self._store.summary)
            except sqlite3.Error, OSError, ValueError:
                self._dropped += len(batch)
                self._error = "Journal write failed; evidence is incomplete."
                try:
                    await asyncio.to_thread(
                        self._store.fail, self._error, self._dropped
                    )
                    self._summary = await asyncio.to_thread(self._store.summary)
                except sqlite3.Error, OSError, ValueError:
                    pass
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def drain(self) -> None:
        await self._queue.join()

    def export(self, kind: str) -> Iterator[str]:
        return self._store.export(kind)

    async def records(self, kind: str, offset: int = 0) -> JournalPage:
        return await asyncio.to_thread(self._store.records, kind, offset)

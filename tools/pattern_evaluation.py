"""Receipt-ordered research reducers. Pure computation; no I/O or delivery."""

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import JsonValue

from tools.pattern_evaluation_events import (
    MARKETS,
    VARIANTS,
    ControlEvidence,
    Envelope,
    HistoryEvidence,
    Market,
    OperatingDecision,
    QuoteEvidence,
    Row,
    Variant,
)


@dataclass(frozen=True)
class Tick:
    time: datetime
    received: datetime
    bid: float
    offer: float

    @property
    def mid(self) -> float:
        return (self.bid + self.offer) / 2

    @property
    def spread(self) -> float:
        return self.offer - self.bid


@dataclass
class Schedule:
    variant: Variant
    inside: set[Market] = field(default_factory=set)
    pending: dict[Market, Row] = field(default_factory=dict)
    cooldown: dict[Market, datetime] = field(default_factory=dict)
    loaded: dict[Market, datetime] = field(default_factory=dict)
    refresh_minute: datetime | None = None

    @property
    def confirmed(self) -> bool:
        return self.variant.startswith("confirmed")

    @property
    def limit(self) -> int:
        return int(self.variant.rsplit("-", 1)[1])


def time_of(row: Row, key: str) -> datetime:
    value = row[key]
    if not isinstance(value, str):
        raise ValueError("Missing timestamp")
    return datetime.fromisoformat(value)


class Evaluator:
    def __init__(
        self, variants: tuple[Variant, ...] = VARIANTS, *, retention: int | None = None
    ):
        self._schedules = [Schedule(variant) for variant in variants]
        self._variants = variants
        self._independent = False
        self._operating_variant: Variant = "confirmed-90"
        self._retention = retention
        self._run = ""
        self._sequence = 0
        self._connected = False
        self._quotes: dict[Market, Tick] = {}
        self._histories: dict[Market, tuple[datetime, float, float, str]] = {}
        self._attempts: list[Row] = []
        self._measurements: list[Row] = []
        self._late: list[Row] = []
        self._coverage: list[Row] = []
        self._coverage_previous: dict[tuple[Market, Variant, bool], datetime] = {}
        self._stages: Counter[tuple[str, Market, str, str, str, str]] = Counter()
        self._counts: Counter[tuple[Market, Variant, str]] = Counter()
        self._limitations: set[str] = set()
        self._observed_decisions = 0
        self._mismatch = False
        self._history_seen = False
        self._last_time: datetime | None = None

    def _stage(
        self,
        market: Market,
        variant: Variant,
        reason: str,
        state: str = "failed",
        unit: str = "quote",
    ) -> None:
        self._stages[(self._run, market, variant, reason, state, unit)] += 1

    def skip(self, event: Envelope) -> None:
        """Account for an excluded phase event without importing its prices."""
        if event.run_id != self._run:
            if self._run:
                self._interrupt("phase_or_recording_boundary")
            self._run, self._sequence = event.run_id, 0
            self._connected = False
            self._schedules = [Schedule(variant) for variant in self._variants]
            self._independent = False
            self._operating_variant = "confirmed-90"
            for schedule in self._schedules:
                schedule.cooldown.clear()
        self._sequence = event.sequence
        if event.kind in ("quote", "history"):
            self._interrupt("outside_phase_or_lookback")

    def _interrupt(self, reason: str, *, keep_history: bool = False) -> None:
        for schedule in self._schedules:
            for attempt in list(schedule.pending.values()):
                self._finish(schedule, attempt, "interrupted", reason, self._last_time)
            schedule.inside.clear()
            if not keep_history:
                schedule.loaded.clear()
                schedule.refresh_minute = None
        for item in self._measurements + self._late:
            if item["status"] == "pending":
                item["status"], item["reason"] = "incomplete", reason
        self._quotes.clear()
        if not keep_history:
            self._histories.clear()
        self._coverage_previous.clear()

    def process(self, event: Envelope) -> None:
        if event.run_id != self._run:
            if self._run:
                self._interrupt("recording_restart")
            self._run, self._sequence = event.run_id, 0
            self._connected = False
            self._schedules = [Schedule(variant) for variant in self._variants]
            self._independent = False
            self._operating_variant = "confirmed-90"
            for schedule in self._schedules:
                schedule.cooldown.clear()
        if event.sequence != self._sequence + 1:
            self._limitations.add(
                "Missing or duplicate causal sequences; continuity is unverified."
            )
            self._interrupt("missing_sequence")
        if event.sequence <= self._sequence:
            return
        self._sequence = event.sequence
        payload = event.payload
        if isinstance(payload, ControlEvidence):
            if payload.reason == "operating_independent_v2":
                self._independent = True
                self._operating_variant = "unfiltered-90"
                self._schedules = [Schedule("unfiltered-90")]
                self._attempts.clear()
                self._measurements.clear()
                self._late.clear()
                self._coverage.clear()
                self._coverage_previous.clear()
                self._stages.clear()
                self._counts.clear()
                self._mismatch = False
                self._observed_decisions = 0
            if payload.reason == "history_failed":
                if self._independent and event.epic is not None:
                    self._histories.pop(event.epic, None)
                    for schedule in self._schedules:
                        schedule.loaded.pop(event.epic, None)
                else:
                    self._histories.clear()
                    for schedule in self._schedules:
                        schedule.loaded.clear()
            if payload.connected is False or payload.reason in (
                "stale_data",
                "recording_failure",
                "recording_interruption",
                "server_shutdown",
                "server_start",
            ):
                self._interrupt(
                    payload.reason, keep_history=payload.reason == "stale_data"
                )
            if payload.connected is not None:
                self._connected = payload.connected
            return
        market = event.epic
        if market is None or event.source_time is None:
            return
        if isinstance(payload, HistoryEvidence):
            if payload.request_sequence >= event.sequence:
                self._limitations.add("History request availability is unverified.")
                return
            if payload.complete:
                high, low = (
                    max(bar.high for bar in payload.bars),
                    min(bar.low for bar in payload.bars),
                )
                minute = payload.as_of.replace(second=0, microsecond=0)
                if high > low:
                    self._histories[market] = (minute, high, low, event.payload_hash)
                    self._history_seen = True
                    for schedule in self._schedules:
                        if not schedule.confirmed:
                            schedule.loaded[market] = minute
                        elif len(self._histories) == 2 and all(
                            value[0] == minute for value in self._histories.values()
                        ):
                            schedule.loaded = dict.fromkeys(MARKETS, minute)
            return
        if isinstance(payload, OperatingDecision):
            self._observed_decisions += 1
            candidates = [
                row
                for row in self._attempts
                if row["run_id"] == event.run_id
                and row["variant"] == self._operating_variant
                and row["market"] == market
                and row["started"] == payload.started.isoformat()
            ]
            expected = "matched" if payload.outcome == "qualified" else payload.outcome
            if not candidates or candidates[-1]["outcome"] != expected:
                self._mismatch = True
            return
        if (
            not isinstance(payload, QuoteEvidence)
            or event.calibrated_received_utc is None
        ):
            return
        time, received = event.source_time, event.calibrated_received_utc
        previous = self._quotes.get(market)
        age = (received - time).total_seconds()
        reason = ""
        if not (
            math.isfinite(payload.bid)
            and math.isfinite(payload.offer)
            and 0 < payload.bid < payload.offer
        ):
            reason = "invalid_quote"
        elif age < -1:
            reason = "future_quote"
        elif age > 30:
            reason = "stale_quote"
            if self._independent:
                for schedule in self._schedules:
                    pending = schedule.pending.get(market)
                    if pending:
                        self._finish(
                            schedule, pending, "interrupted", "stale_data", time
                        )
                    schedule.inside.discard(market)
                self._quotes.pop(market, None)
                for item in self._measurements + self._late:
                    if item["market"] == market and item["status"] == "pending":
                        item.update(status="incomplete", reason="stale_data")
            else:
                self._interrupt("stale_data", keep_history=True)
        elif not self._connected:
            reason = "feed_paused"
        elif previous and time <= previous.time:
            reason = "out_of_order"
        if reason:
            for schedule in self._schedules:
                self._stage(market, schedule.variant, reason)
            return
        tick = Tick(time, received, payload.bid, payload.offer)
        self._last_time = time
        self._quotes[market] = tick
        other_market: Market = "OIL_CRUDE" if market == "GOLD" else "GOLD"
        other = self._quotes.get(other_market)
        aligned = (
            other is not None
            and 0 <= (time - other.time).total_seconds() <= 5
            and (received - other.time).total_seconds() <= 30
        )
        reference = self._histories.get(market)
        minute = time.replace(second=0, microsecond=0)
        for schedule in self._schedules:
            self._stage(market, schedule.variant, "accepted", "eligible")
            # Every accepted market event can expire every open market attempt.
            for attempt in list(schedule.pending.values()):
                if self._independent and attempt["market"] != market:
                    continue
                if time > time_of(attempt, "deadline"):
                    self._finish(
                        schedule, attempt, "expired", "Return took too long", time
                    )
            pending = schedule.pending.get(market)
            has_history = pending is not None or (
                reference is not None
                and reference[0] == minute
                and schedule.loaded.get(market) == minute
            )
            # Exposure includes pending attempts and cooldowns. The interval must
            # be eligible at both received endpoints; late history cannot backfill it.
            for paired in (False,) if self._independent else (False, True):
                key = (market, schedule.variant, paired)
                other_reference = self._histories.get(other_market)
                eligible = has_history and (
                    not paired
                    or (
                        aligned
                        and other_reference is not None
                        and other_reference[0] == minute
                    )
                )
                start = self._coverage_previous.get(key)
                if (
                    eligible
                    and start is not None
                    and 0 < (time - start).total_seconds() <= 5
                ):
                    self._add_coverage(market, schedule.variant, start, time, paired)
                if eligible:
                    self._coverage_previous[key] = time
                else:
                    self._coverage_previous.pop(key, None)
            if pending:
                low, high = pending["range_low"], pending["range_high"]
                if (
                    isinstance(low, (int, float))
                    and isinstance(high, (int, float))
                    and low < tick.mid < high
                ):
                    other_start = pending["other_start"]
                    qualified = not schedule.confirmed or (
                        aligned
                        and other is not None
                        and isinstance(other_start, (int, float))
                        and (other.mid - other_start) * int(str(pending["sign"])) < 0
                    )
                    pending.update(
                        return_time=time.isoformat(),
                        return_sequence=event.sequence,
                        return_bid=tick.bid,
                        return_offer=tick.offer,
                        return_spread=tick.spread,
                    )
                    self._finish(
                        schedule,
                        pending,
                        "matched" if qualified else "context_failed",
                        "Return confirmed"
                        if qualified
                        else "Other market unavailable or did not move oppositely",
                        time,
                    )
                    self._start_measurements(pending, tick)
                continue
            # A refresh-causing quote is skipped, just like the operating detector.
            if schedule.loaded.get(market) != minute:
                if schedule.refresh_minute != minute:
                    if self._independent:
                        schedule.inside.discard(market)
                        schedule.loaded.pop(market, None)
                    else:
                        schedule.inside.clear()
                        schedule.loaded.clear()
                    schedule.refresh_minute = minute
                self._stage(market, schedule.variant, "history", "unknown")
                continue
            if reference is None or reference[0] != minute:
                self._stage(market, schedule.variant, "history", "unknown")
                continue
            if schedule.confirmed and (
                len(self._histories) != 2
                or any(value[0] != minute for value in self._histories.values())
            ):
                self._stage(market, schedule.variant, "history", "unknown")
                continue
            if time < schedule.cooldown.get(market, time):
                self._stage(market, schedule.variant, "cooldown")
                continue
            _, high, low, history_hash = reference
            if low < tick.mid < high:
                schedule.inside.add(market)
                self._stage(market, schedule.variant, "inside_observation", "eligible")
                continue
            if market not in schedule.inside:
                self._stage(market, schedule.variant, "inside_observation")
                continue
            sign = (
                1
                if tick.mid > high + 2 * tick.spread
                else -1
                if tick.mid < low - 2 * tick.spread
                else 0
            )
            if not sign:
                self._stage(market, schedule.variant, "threshold")
                continue
            self._stage(market, schedule.variant, "threshold", "eligible", "candidate")
            if schedule.confirmed and (not aligned or other is None):
                self._stage(
                    market,
                    schedule.variant,
                    "counterpart_availability",
                    "failed",
                    "candidate",
                )
                continue
            attempt: Row = {
                "attempt_id": f"{self._run}:{schedule.variant}:{market}:{event.sequence}",
                "run_id": self._run,
                "market": market,
                "variant": schedule.variant,
                "phase": "development",
                "start_sequence": event.sequence,
                "started": time.isoformat(),
                "observed_time": event.host_observed_utc.isoformat(),
                "deadline": (time + timedelta(seconds=schedule.limit)).isoformat(),
                "limit_seconds": schedule.limit,
                "sign": sign,
                "range_high": high,
                "range_low": low,
                "history_hash": history_hash,
                "breakout_price": tick.mid,
                "other_start": other.mid if other and not self._independent else None,
                "outcome": "open",
                "reason": "Waiting for a return inside",
                "return_time": None,
            }
            schedule.pending[market] = attempt
            schedule.inside.discard(market)
            self._attempts.append(attempt)
            self._counts[(market, schedule.variant, "starts")] += 1
            self._stage(
                market, schedule.variant, "attempt_started", "eligible", "attempt"
            )
        self._advance_followups(market, tick, previous, event.sequence)
        self._trim()

    def _finish(
        self,
        schedule: Schedule,
        attempt: Row,
        outcome: str,
        reason: str,
        time: datetime | None,
    ) -> None:
        value = attempt["market"]
        if value == "GOLD":
            market: Market = "GOLD"
        elif value == "OIL_CRUDE":
            market = "OIL_CRUDE"
        else:
            raise ValueError("Invalid attempt market")
        attempt.update(
            outcome=outcome,
            reason=reason,
            ended=time.isoformat() if time else None,
            decision_sequence=self._sequence,
        )
        schedule.pending.pop(market, None)
        schedule.inside.discard(market)
        self._counts[(market, schedule.variant, outcome)] += 1
        self._stage(
            market,
            schedule.variant,
            outcome,
            "eligible" if outcome == "matched" else "failed",
            "attempt",
        )
        if outcome == "matched" and time:
            schedule.cooldown[market] = time + timedelta(seconds=90)
        if outcome == "expired":
            self._late.append(
                {
                    "attempt_id": attempt["attempt_id"],
                    "market": market,
                    "variant": schedule.variant,
                    "phase": attempt["phase"],
                    "deadline": attempt["deadline"],
                    "started": attempt["started"],
                    "end": (
                        time_of(attempt, "deadline") + timedelta(seconds=300)
                    ).isoformat(),
                    "range_high": attempt["range_high"],
                    "range_low": attempt["range_low"],
                    "status": "pending",
                    "reason": "Awaiting complete five-minute observation",
                    "first_return": None,
                    "elapsed_seconds": None,
                }
            )

    def _start_measurements(self, attempt: Row, tick: Tick) -> None:
        # Reversal reference direction: upper failed breakout is DOWN.
        direction = -int(str(attempt["sign"]))
        entry = tick.offer if direction == 1 else tick.bid
        for horizon in (60, 180, 300):
            self._measurements.append(
                {
                    "attempt_id": attempt["attempt_id"],
                    "market": attempt["market"],
                    "variant": attempt["variant"],
                    "phase": attempt["phase"],
                    "return_time": tick.time.isoformat(),
                    "horizon_seconds": horizon,
                    "due": (tick.time + timedelta(seconds=horizon)).isoformat(),
                    "direction": direction,
                    "entry": entry,
                    "entry_spread": tick.spread,
                    "exit": None,
                    "exit_spread": None,
                    "points": None,
                    "best": -tick.spread,
                    "worst": -tick.spread,
                    "status": "pending",
                    "reason": "Awaiting future observations",
                    "eligible_primary_match": attempt["outcome"] == "matched"
                    and horizon == 180,
                    "last_time": tick.time.isoformat(),
                }
            )

    def _advance_followups(
        self, market: Market, tick: Tick, previous: Tick | None, sequence: int
    ) -> None:
        for item in self._measurements:
            if item["market"] != market or item["status"] != "pending":
                continue
            gap = (tick.time - time_of(item, "last_time")).total_seconds()
            if gap > 5:
                item.update(
                    status="incomplete", reason="Price gap exceeded five seconds"
                )
                continue
            direction = int(str(item["direction"]))
            exit_value = tick.bid if direction == 1 else tick.offer
            entry = item["entry"]
            if not isinstance(entry, (int, float)):
                raise ValueError("Invalid measurement entry")
            movement = direction * (exit_value - entry)
            due = time_of(item, "due")
            if tick.time <= due:
                item["best"] = max(float(str(item["best"])), movement)
                item["worst"] = min(float(str(item["worst"])), movement)
            item["last_time"] = tick.time.isoformat()
            if tick.time >= due:
                if (tick.time - due).total_seconds() <= 5:
                    item.update(
                        status="complete",
                        reason="Observed quote-side movement; not account profit",
                        exit=exit_value,
                        exit_spread=tick.spread,
                        points=movement,
                        exit_time=tick.time.isoformat(),
                        exit_sequence=sequence,
                    )
                else:
                    item.update(
                        status="incomplete",
                        reason="No horizon quote within five seconds",
                    )
        for item in self._late:
            if item["market"] != market:
                continue
            deadline, end = time_of(item, "deadline"), time_of(item, "end")
            if tick.time <= deadline:
                continue
            if item["status"] == "complete":
                continue
            if item["status"] == "pending" and (
                previous is None or (tick.time - previous.time).total_seconds() > 5
            ):
                item.update(
                    status="incomplete",
                    reason="Missing target observations in late window",
                )
            low, high = item["range_low"], item["range_high"]
            if (
                tick.time <= end
                and item["first_return"] is None
                and isinstance(low, (int, float))
                and isinstance(high, (int, float))
                and low < tick.mid < high
            ):
                item.update(
                    first_return=tick.time.isoformat(),
                    elapsed_seconds=(
                        tick.time - time_of(item, "started")
                    ).total_seconds(),
                    return_sequence=sequence,
                )
            if tick.time >= end and item["status"] == "pending":
                item.update(
                    status="complete",
                    reason="Observed late return"
                    if item["first_return"]
                    else "no_observed_return",
                )

    def _add_coverage(
        self,
        market: Market,
        variant: Variant,
        start: datetime,
        end: datetime,
        paired: bool,
    ) -> None:
        for row in reversed(self._coverage):
            if (
                row["run_id"] == self._run
                and row["market"] == market
                and row["variant"] == variant
                and row["paired"] == paired
            ):
                if time_of(row, "end") >= start:
                    row["end"] = max(time_of(row, "end"), end).isoformat()
                    row["seconds"] = (
                        time_of(row, "end") - time_of(row, "start")
                    ).total_seconds()
                    return
                break
        self._coverage.append(
            {
                "run_id": self._run,
                "market": market,
                "variant": variant,
                "phase": "development",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "paired": paired,
                "seconds": (end - start).total_seconds(),
                "sessions_verified": False,
            }
        )

    def _trim(self) -> None:
        if self._retention is not None:
            for records in (
                self._attempts,
                self._measurements,
                self._late,
                self._coverage,
            ):
                if len(records) > self._retention:
                    del records[: len(records) - self._retention]

    def parity(self) -> str:
        return (
            "baseline_mismatch"
            if self._mismatch
            else "verified"
            if self._observed_decisions
            else "not_verified"
        )

    def records(self) -> dict[str, list[Row]]:
        stages: list[Row] = [
            {
                "run_id": run,
                "market": market,
                "variant": variant,
                "phase": "development",
                "reason": reason,
                "state": state,
                "unit": unit,
                "count": count,
            }
            for (run, market, variant, reason, state, unit), count in sorted(
                self._stages.items()
            )
        ]
        groups: list[Row] = []
        for market in MARKETS:
            for schedule in self._schedules:
                variant = schedule.variant
                measurements = [
                    row
                    for row in self._measurements
                    if row["market"] == market and row["variant"] == variant
                ]
                groups.append(
                    {
                        "market": market,
                        "variant": variant,
                        "phase": "development",
                        "starts": self._counts[(market, variant, "starts")],
                        "matches": self._counts[(market, variant, "matched")],
                        "expired": self._counts[(market, variant, "expired")],
                        "context_failed": self._counts[
                            (market, variant, "context_failed")
                        ],
                        "complete_measurements": sum(
                            row["status"] == "complete" for row in measurements
                        ),
                        "incomplete_measurements": sum(
                            row["status"] != "complete" for row in measurements
                        ),
                        "target_hours": sum(
                            float(str(row["seconds"]))
                            for row in self._coverage
                            if row["market"] == market
                            and row["variant"] == variant
                            and not row["paired"]
                        )
                        / 3600,
                        "paired_hours": sum(
                            float(str(row["seconds"]))
                            for row in self._coverage
                            if row["market"] == market
                            and row["variant"] == variant
                            and row["paired"]
                        )
                        / 3600,
                        "normal_mean": None,
                        "doubled_mean": None,
                        "decision": "insufficient",
                        "reason": "Development evidence; complete sessions, costs and prospective validation required",
                    }
                )
        return {
            "coverage": [dict(row) for row in self._coverage],
            "stages": stages,
            "attempts": [dict(row) for row in self._attempts],
            "followups": [dict(row) for row in self._measurements],
            "late_returns": [dict(row) for row in self._late],
            "groups": groups,
        }

    def summary(self) -> dict[str, JsonValue]:
        return {
            "independent": self._independent,
            "rule_version": "independent-rejection-v2"
            if self._independent
            else "paired-rejection-v1",
            "run_id": self._run,
            "processed_through_sequence": self._sequence,
            "processed_through_time": self._last_time.isoformat()
            if self._last_time
            else None,
            "attempts": sum(
                count
                for (_, _, kind), count in self._counts.items()
                if kind == "starts"
            ),
            "accepted_quotes": sum(
                count
                for (_, _, _, reason, _, _), count in self._stages.items()
                if reason == "accepted"
            ),
            "parity": self.parity(),
            "limitations": sorted(
                self._limitations
                | (
                    {"Reference history unavailable; historical checks are unknown."}
                    if not self._history_seen
                    else set()
                )
            ),
            "latest_attempt": dict(self._attempts[-1]) if self._attempts else None,
        }

"""Neutral, allowlisted research evidence. No broker or delivery capabilities."""

import hashlib
import json
import math
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

type Market = Literal["GOLD", "OIL_CRUDE"]
type Variant = Literal[
    "confirmed-90", "unfiltered-90", "confirmed-180", "unfiltered-180"
]
type Cell = str | int | float | bool | None
type Row = dict[str, Cell]
VARIANTS: tuple[Variant, ...] = (
    "confirmed-90",
    "unfiltered-90",
    "confirmed-180",
    "unfiltered-180",
)
MARKETS: tuple[Market, ...] = ("GOLD", "OIL_CRUDE")
RULE_VERSION = "evaluation-v1"


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Bar(Record):
    time: datetime
    open: float
    high: float
    low: float
    close: float

    @model_validator(mode="after")
    def valid(self):
        if (
            self.time.utcoffset() is None
            or not 0
            < self.low
            <= min(self.open, self.close)
            <= max(self.open, self.close)
            <= self.high
        ):
            raise ValueError("Invalid UTC reference bar")
        return self


class QuoteEvidence(Record):
    bid: float
    offer: float
    quality: str


class HistoryEvidence(Record):
    request_id: str = Field(min_length=1, max_length=128)
    as_of: datetime
    bars: tuple[Bar, ...]
    complete: bool
    request_sequence: int = Field(ge=0)

    @model_validator(mode="after")
    def valid(self):
        if self.as_of.utcoffset() is None:
            raise ValueError("History requires aware time")
        required = {
            self.as_of.replace(second=0, microsecond=0) - timedelta(minutes=i)
            for i in range(1, 21)
        }
        if self.complete and (
            len(self.bars) != 20 or {bar.time for bar in self.bars} != required
        ):
            raise ValueError("Complete history requires exact 20 preceding bars")
        return self


class ControlEvidence(Record):
    reason: str
    connected: bool | None = None
    clock_offset_seconds: float | None = None


class OperatingDecision(Record):
    started: datetime
    outcome: str


class Envelope(Record):
    schema_version: Literal[1] = 1
    source_id: str = "capital-demo"
    capture_version: Literal["evaluation-v1"] = RULE_VERSION
    run_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    sequence: int = Field(ge=1)
    kind: Literal["quote", "history", "control", "decision"]
    epic: Market | None = None
    source_time: datetime | None = None
    host_observed_utc: datetime
    calibrated_received_utc: datetime | None = None
    calibration_id: str | None = None
    payload_hash: str = ""
    payload: QuoteEvidence | HistoryEvidence | ControlEvidence | OperatingDecision

    @model_validator(mode="after")
    def valid(self):
        owners = {
            "quote": QuoteEvidence,
            "history": HistoryEvidence,
            "control": ControlEvidence,
            "decision": OperatingDecision,
        }
        if not isinstance(self.payload, owners[self.kind]):
            raise ValueError("Evidence kind/payload mismatch")
        for time in (
            self.host_observed_utc,
            self.source_time,
            self.calibrated_received_utc,
        ):
            if time is not None and time.utcoffset() is None:
                raise ValueError("Evidence timestamps must include timezone")
        if self.kind != "control" and (self.epic is None or self.source_time is None):
            raise ValueError("Market evidence requires market and source time")
        if self.kind == "quote" and self.calibrated_received_utc is None:
            raise ValueError("Quotes require calibrated receipt time")
        expected = digest(self.payload.model_dump(mode="json"))
        if self.payload_hash and self.payload_hash != expected:
            raise ValueError("Evidence payload hash mismatch")
        object.__setattr__(self, "payload_hash", expected)
        return self


class Session(Record):
    market: Market
    start: datetime
    end: datetime
    provenance: str = Field(min_length=1)

    @model_validator(mode="after")
    def valid(self):
        if (
            self.start.utcoffset() is None
            or self.end.utcoffset() is None
            or self.start >= self.end
        ):
            raise ValueError("Invalid sourced session")
        return self


class Costs(Record):
    commission: float = Field(ge=0)
    slippage: float = Field(ge=0)
    funding: float = Field(ge=0)
    adjustment: float = Field(ge=0)
    provenance: str = Field(min_length=1)
    timely_execution: bool = False

    def total(self) -> float:
        return self.commission + self.slippage + self.funding + self.adjustment


class Manifest(Record):
    schema_version: Literal[1] = 1
    label: Literal["synthetic", "development", "validation"]
    source_id: str = Field(min_length=1)
    capture_version: Literal["evaluation-v1"] = RULE_VERSION
    frozen_at: datetime
    development_start: datetime
    development_end: datetime
    validation_start: datetime | None = None
    validation_end: datetime | None = None
    variants: tuple[Variant, ...] = VARIANTS
    inspected_hashes: tuple[str, ...] = ()
    sessions: tuple[Session, ...] = ()
    costs: Costs | None = None
    primary_horizon: Literal[180] = 180
    seed: Literal[204] = 204
    replicates: Literal[50000] = 50000

    @model_validator(mode="after")
    def valid(self):
        if len(self.variants) != 4 or set(self.variants) != set(VARIANTS):
            raise ValueError("Exactly four distinct frozen variants required")
        for time in (
            self.frozen_at,
            self.development_start,
            self.development_end,
            self.validation_start,
            self.validation_end,
        ):
            if time is not None and time.utcoffset() is None:
                raise ValueError("Manifest dates must include timezone")
        if self.development_start >= self.development_end:
            raise ValueError("Invalid development boundaries")
        if (self.validation_start is None) != (self.validation_end is None):
            raise ValueError("Both validation bounds required")
        if self.validation_start is not None and self.validation_end is not None:
            if (
                self.validation_end <= self.validation_start
                or self.validation_start - self.development_end < timedelta(minutes=8)
            ):
                raise ValueError("Validation requires eight-minute embargo")
            if self.frozen_at >= self.validation_start:
                raise ValueError("Validation must be frozen prospectively")
        if self.label == "validation" and (
            self.validation_start is None or not self.sessions or self.costs is None
        ):
            raise ValueError(
                "Formal validation requires period, sourced sessions and measured costs"
            )
        return self

    def identity(self) -> str:
        return digest(self.model_dump(mode="json"))


class RunCutoff(Record):
    run_id: str
    sequence: int = Field(ge=1)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvidenceSnapshot(Record):
    schema_version: Literal[1] = 1
    definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cutoffs: tuple[RunCutoff, ...]
    observed_through: datetime | None
    source_ids: tuple[str, ...]
    capture_states: tuple[tuple[str, str], ...] = ()
    limitations: tuple[str, ...] = ()

    def identity(self) -> str:
        return digest(self.model_dump(mode="json"))


def finite_cells(row: Row) -> Row:
    return {
        key: None if isinstance(value, float) and not math.isfinite(value) else value
        for key, value in row.items()
    }

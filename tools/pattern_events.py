"""Typed research observations shared by detection and journal storage."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class QuoteRecord:
    epic: str
    time: datetime
    received: datetime
    bid: float
    offer: float
    quality: str


@dataclass(frozen=True)
class RangeRecord:
    epic: str
    time: datetime
    high: float
    low: float


@dataclass(frozen=True)
class AttemptRecord:
    epic: str
    started: datetime
    sign: int
    range_high: float
    range_low: float
    breakout_price: float
    other_start: float | None


@dataclass(frozen=True)
class DecisionRecord:
    epic: str
    started: datetime
    time: datetime
    outcome: str
    reason: str
    bid: float | None = None
    offer: float | None = None
    other_end: float | None = None


@dataclass(frozen=True)
class GapRecord:
    """A recording interruption observed on the host UTC clock."""

    time: datetime
    reason: str


type ResearchRecord = (
    QuoteRecord | RangeRecord | AttemptRecord | DecisionRecord | GapRecord
)

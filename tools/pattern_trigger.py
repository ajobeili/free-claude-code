"""Live quote breakout/rejection detector. Candles supply reference levels only."""

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta

import httpx

from tools.capital_api import CapitalAPI
from tools.pattern_evaluation_events import Bar
from tools.pattern_events import (
    AttemptRecord,
    DecisionRecord,
    GapRecord,
    QuoteRecord,
    RangeRecord,
    ResearchRecord,
)
from tools.pattern_study import timestamp

EPICS = ("GOLD", "OIL_CRUDE")
NAMES = {"GOLD": "Gold", "OIL_CRUDE": "Crude oil"}


@dataclass(frozen=True)
class PatternSettings:
    independent: bool = False
    rejection_seconds: int = 90
    spread_multiple: float = 2
    quote_fresh_seconds: int = 5
    max_research_delay_seconds: int = 30
    cooldown_seconds: int = 90


@dataclass(frozen=True)
class ReferenceRange:
    high: float
    low: float

    def __post_init__(self):
        if not (
            math.isfinite(self.high)
            and math.isfinite(self.low)
            and 0 < self.low < self.high
        ):
            raise ValueError("Invalid reference range")


@dataclass(frozen=True)
class Quote:
    time: datetime
    mid: float
    spread: float


@dataclass(frozen=True)
class Breakout:
    time: datetime
    sign: int
    price: float
    reference: ReferenceRange
    other_start: float | None
    extreme: float | None = None


@dataclass(frozen=True)
class PatternAlert:
    epic: str
    market: str
    direction: str
    trigger_time: str
    breakout_time: str
    range_high: float
    range_low: float
    breakout_price: float
    return_price: float
    spread: float
    other_market: str
    other_start: float | None
    other_end: float | None
    data_mode: str = "RESEARCH"
    quote_delay_seconds: float = 0
    observed_time: str = ""
    action: str = "RESEARCH ALERT"
    rule_version: str = "paired-rejection-v1"
    breakout_extreme: float | None = None


@dataclass(frozen=True)
class MarketView:
    epic: str
    market: str
    phase: str = "collecting"
    range_high: float | None = None
    range_low: float | None = None
    price: float | None = None
    detail: str = "Waiting for 20 consecutive completed one-minute candles."
    quote_time: str | None = None


@dataclass(frozen=True)
class TriggerState:
    independent: bool = False
    rule_version: str = "paired-rejection-v1"
    status: str = "warming"
    last_quote: str | None = None
    last_alert: str | None = None
    error: str = ""
    markets: list[MarketView] = field(default_factory=list)
    alerts: list[PatternAlert] = field(default_factory=list)
    version: int = 0
    feed_connected: bool = False
    quote_delay_seconds: float | None = None
    delayed_research: bool = False


class PatternTrigger:
    def __init__(
        self,
        history: Callable[[datetime], Awaitable[dict[str, ReferenceRange]]],
        settings: PatternSettings = PatternSettings(),
        on_alert: Callable[[PatternAlert], None] | None = None,
        on_record: Callable[[ResearchRecord], None] | None = None,
        on_feed: Callable[[bool], None] | None = None,
        market_history: Callable[[str, datetime], Awaitable[dict[str, ReferenceRange]]]
        | None = None,
    ):
        self._history = history
        self._market_history = market_history
        if settings.independent and market_history is None:
            raise ValueError(
                "Independent monitoring requires a market-owned history loader"
            )
        self._settings = settings
        self._on_alert = on_alert
        self._on_record = on_record
        self._on_feed = on_feed
        self._state = TriggerState(
            markets=[MarketView(e, NAMES[e]) for e in EPICS],
            independent=settings.independent,
            rule_version="independent-rejection-v2"
            if settings.independent
            else "paired-rejection-v1",
        )
        self._condition = asyncio.Condition()
        self._lock = asyncio.Lock()
        self._ranges: dict[str, ReferenceRange] = {}
        self._quotes: dict[str, Quote] = {}
        self._inside: set[str] = set()
        self._pending: dict[str, Breakout] = {}
        self._cooldown: dict[str, datetime] = {}
        self._loaded_minute: datetime | None = None
        self._attempt_minute: datetime | None = None
        self._attempt_time: datetime | None = None
        self._attempts = 0
        self._delay_seconds: float | None = None
        self._market_loaded: dict[str, datetime] = {}
        self._market_attempts: dict[str, tuple[datetime, int, datetime]] = {}
        self._paused: set[str] = set()

    def snapshot(self) -> TriggerState:
        return self._state

    def _record(self, event: ResearchRecord) -> None:
        if self._on_record is not None:
            self._on_record(event)

    async def feed_status(self, connected: bool) -> None:
        async with self._lock:
            if self._state.feed_connected == connected:
                return
            if self._on_feed is not None:
                self._on_feed(connected)
            if not connected:
                self._record(GapRecord(datetime.now(UTC), "feed_disconnected"))
                self._delay_seconds = None
                self._pending.clear()
                self._quotes.clear()
                self._inside.clear()
                self._ranges.clear()
                self._loaded_minute = None
                self._attempt_minute = None
                self._market_loaded.clear()
                self._market_attempts.clear()
                self._paused.clear()
            await self._publish(feed_connected=connected)

    async def _publish(self, **changes: object) -> None:
        views = []
        for epic in EPICS:
            pending = self._pending.get(epic)
            reference = pending.reference if pending else self._ranges.get(epic)
            quote = self._quotes.get(epic)
            phase = (
                "paused"
                if epic in self._paused
                else "breakout"
                if pending
                else "watching"
                if reference
                else "collecting"
            )
            detail = (
                f"Breakout {'above' if pending.sign == 1 else 'below'} the range. "
                "Watching for a return inside within 90 seconds."
                if pending
                else "Watching received prices for a breakout beyond twice the spread."
                if reference
                else "Waiting for 20 consecutive completed one-minute candles."
            )
            views.append(
                MarketView(
                    epic,
                    NAMES[epic],
                    phase,
                    reference.high if reference else None,
                    reference.low if reference else None,
                    quote.mid if quote else None,
                    detail,
                    quote.time.isoformat() if quote else None,
                )
            )
        async with self._condition:
            self._state = replace(
                self._state,
                markets=views,
                **changes,
                version=self._state.version + 1,
                quote_delay_seconds=self._delay_seconds,
                delayed_research=self._delay_seconds is not None
                and self._delay_seconds > self._settings.quote_fresh_seconds,
            )
            self._condition.notify_all()

    async def events(self) -> AsyncIterator[TriggerState]:
        version = -1
        while True:
            async with self._condition:
                await self._condition.wait_for(
                    lambda expected=version: self._state.version != expected
                )
                state = self._state
            version = state.version
            yield state

    async def _refresh(self, time: datetime, epic: str) -> bool:
        if self._settings.independent:
            return await self._refresh_market(time, epic)
        minute = time.replace(second=0, microsecond=0)
        if minute == self._loaded_minute:
            return True
        if minute != self._attempt_minute:
            self._attempt_minute, self._attempts = minute, 0
            self._ranges.clear()
            self._inside.clear()
        if self._attempts >= 3 or (
            self._attempt_time is not None
            and (time - self._attempt_time).total_seconds() < 1
        ):
            return False
        self._attempts += 1
        self._attempt_time = time
        try:
            async with asyncio.timeout(10):
                ranges = await self._history(time)
        except (
            OSError,
            ValueError,
            RuntimeError,
            TimeoutError,
            KeyError,
            TypeError,
            ArithmeticError,
            httpx.HTTPError,
        ):
            await self._publish(
                status="warming",
                error="Reference history unavailable; retrying on price updates.",
            )
            return False
        self._ranges = ranges
        for epic, reference in ranges.items():
            self._record(RangeRecord(epic, time, reference.high, reference.low))
        self._loaded_minute = minute if len(ranges) == 2 else None
        await self._publish(
            status="watching" if len(ranges) == 2 else "warming", error=""
        )
        # A history request never fires an alert. The next fresh quote evaluates it.
        return False

    async def _refresh_market(self, time: datetime, epic: str) -> bool:
        minute = time.replace(second=0, microsecond=0)
        if self._market_loaded.get(epic) == minute:
            return True
        previous = self._market_attempts.get(epic)
        attempts = previous[1] if previous and previous[0] == minute else 0
        if not previous or previous[0] != minute:
            self._ranges.pop(epic, None)
            self._inside.discard(epic)
        if attempts >= 3 or (previous and (time - previous[2]).total_seconds() < 1):
            return False
        self._market_attempts[epic] = (minute, attempts + 1, time)
        loader = self._market_history
        if loader is None:
            raise RuntimeError("Market history loader unavailable")
        try:
            async with asyncio.timeout(10):
                ranges = await loader(epic, time)
        except (
            OSError,
            ValueError,
            RuntimeError,
            TimeoutError,
            KeyError,
            TypeError,
            ArithmeticError,
            httpx.HTTPError,
        ):
            self._paused.add(epic)
            await self._publish(status="watching", error="")
            return False
        reference = ranges.get(epic)
        if reference is not None:
            self._ranges[epic] = reference
            self._market_loaded[epic] = minute
            self._paused.discard(epic)
            self._record(RangeRecord(epic, time, reference.high, reference.low))
        else:
            self._paused.add(epic)
        await self._publish(status="watching", error="")
        return False

    async def on_quote(
        self,
        epic: str,
        quote_time: datetime,
        received: datetime,
        bid: float,
        offer: float,
    ) -> None:
        if (
            epic not in EPICS
            or quote_time.utcoffset() is None
            or received.utcoffset() is None
            or not math.isfinite(bid)
            or not math.isfinite(offer)
            or not 0 < bid < offer
        ):
            return
        quote_time, received = quote_time.astimezone(UTC), received.astimezone(UTC)
        age = (received - quote_time).total_seconds()
        if age < -1:
            self._record(QuoteRecord(epic, quote_time, received, bid, offer, "future"))
            return
        async with self._lock:
            self._delay_seconds = max(0, age)
            if age > self._settings.max_research_delay_seconds:
                self._record(
                    QuoteRecord(epic, quote_time, received, bid, offer, "too_old")
                )
                if self._settings.independent:
                    pending = self._pending.pop(epic, None)
                    if pending is not None:
                        self._record(
                            DecisionRecord(
                                epic,
                                pending.time,
                                quote_time,
                                "interrupted",
                                "This market's quote exceeded the 30-second delay limit.",
                            )
                        )
                    self._inside.discard(epic)
                    self._quotes.pop(epic, None)
                    self._paused.add(epic)
                    await self._publish(status="watching", error="")
                    return
                if self._state.status != "delayed":
                    self._record(GapRecord(datetime.now(UTC), "stale_data"))
                    self._pending.clear()
                    self._inside.clear()
                    self._quotes.clear()
                    await self._publish(
                        status="delayed",
                        error="Quotes exceed the 30-second research delay limit. Alerts paused until newer data arrives.",
                    )
                return
            previous = self._quotes.get(epic)
            if not self._state.feed_connected or (
                previous and quote_time <= previous.time
            ):
                self._record(
                    QuoteRecord(
                        epic,
                        quote_time,
                        received,
                        bid,
                        offer,
                        "feed_paused"
                        if not self._state.feed_connected
                        else "out_of_order",
                    )
                )
                return
            self._record(
                QuoteRecord(
                    epic,
                    quote_time,
                    received,
                    bid,
                    offer,
                    "delayed_research"
                    if age > self._settings.quote_fresh_seconds
                    else "timely",
                )
            )
            quote = Quote(quote_time, (bid + offer) / 2, offer - bid)
            self._quotes[epic] = quote
            self._paused.discard(epic)
            if self._state.status == "delayed":
                await self._publish(
                    status="watching" if self._ranges else "warming", error=""
                )
            for item, pending in list(self._pending.items()):
                if self._settings.independent and item != epic:
                    continue
                if (
                    quote_time - pending.time
                ).total_seconds() > self._settings.rejection_seconds:
                    self._record(
                        DecisionRecord(
                            item,
                            pending.time,
                            quote_time,
                            "expired",
                            "No return inside the frozen range within 90 seconds.",
                        )
                    )
                    del self._pending[item]
                    self._inside.discard(item)
            # A pending rejection uses its frozen range, even across a minute boundary.
            if epic not in self._pending and not await self._refresh(quote_time, epic):
                return
            reference = self._ranges.get(epic)
            other_epic = EPICS[1 - EPICS.index(epic)]
            other = self._quotes.get(other_epic)
            other_fresh = (
                other is not None
                and 0
                <= (quote_time - other.time).total_seconds()
                <= self._settings.quote_fresh_seconds
                and (received - other.time).total_seconds()
                <= self._settings.max_research_delay_seconds
            )
            pending = self._pending.get(epic)
            if pending:
                previous_extreme = (
                    pending.extreme if pending.extreme is not None else pending.price
                )
                pending = replace(
                    pending,
                    extreme=max(previous_extreme, quote.mid)
                    if pending.sign == 1
                    else min(previous_extreme, quote.mid),
                )
                self._pending[epic] = pending
                inside = pending.reference.low < quote.mid < pending.reference.high
                if inside:
                    del self._pending[epic]
                    self._inside.discard(epic)
                    qualified = self._settings.independent or (
                        other_fresh
                        and other is not None
                        and pending.other_start is not None
                        and (other.mid - pending.other_start) * pending.sign < 0
                    )
                    self._record(
                        DecisionRecord(
                            epic,
                            pending.time,
                            quote_time,
                            "qualified" if qualified else "context_failed",
                            "Return inside this market's own range confirmed."
                            if self._settings.independent
                            else "Reentry and opposite-market movement confirmed."
                            if qualified
                            else "Other market quote was unavailable or misaligned."
                            if not other_fresh
                            else "Other market did not move oppositely during the attempt.",
                            bid,
                            offer,
                            other.mid
                            if other and not self._settings.independent
                            else None,
                        )
                    )
                    if qualified:
                        delay = (
                            age
                            if self._settings.independent or other is None
                            else max(age, (received - other.time).total_seconds())
                        )
                        alert = PatternAlert(
                            epic,
                            NAMES[epic],
                            "DOWN" if pending.sign == 1 else "UP",
                            quote_time.isoformat(),
                            pending.time.isoformat(),
                            pending.reference.high,
                            pending.reference.low,
                            pending.price,
                            quote.mid,
                            quote.spread,
                            "" if self._settings.independent else NAMES[other_epic],
                            pending.other_start,
                            other.mid
                            if other and not self._settings.independent
                            else None,
                            data_mode="DELAYED RESEARCH"
                            if delay > self._settings.quote_fresh_seconds
                            else "RESEARCH",
                            quote_delay_seconds=max(0, delay),
                            observed_time=received.isoformat(),
                            rule_version=self._state.rule_version,
                            breakout_extreme=pending.extreme,
                        )
                        self._cooldown[epic] = quote_time + timedelta(
                            seconds=self._settings.cooldown_seconds
                        )
                        await self._publish(
                            status="watching",
                            last_quote=quote_time.isoformat(),
                            last_alert=quote_time.isoformat(),
                            alerts=[alert, *self._state.alerts][:20],
                        )
                        if self._on_alert is not None:
                            self._on_alert(alert)
                        return
            elif reference and quote_time >= self._cooldown.get(epic, quote_time):
                if reference.low < quote.mid < reference.high:
                    self._inside.add(epic)
                elif epic in self._inside and (
                    self._settings.independent or (other_fresh and other is not None)
                ):
                    buffer = self._settings.spread_multiple * quote.spread
                    sign = (
                        1
                        if quote.mid > reference.high + buffer
                        else -1
                        if quote.mid < reference.low - buffer
                        else 0
                    )
                    if sign:
                        self._pending[epic] = Breakout(
                            quote_time,
                            sign,
                            quote.mid,
                            reference,
                            other.mid
                            if other and not self._settings.independent
                            else None,
                        )
                        self._record(
                            AttemptRecord(
                                epic,
                                quote_time,
                                sign,
                                reference.high,
                                reference.low,
                                quote.mid,
                                other.mid
                                if other and not self._settings.independent
                                else None,
                            )
                        )
                        self._inside.discard(epic)
            await self._publish(last_quote=quote_time.isoformat())


async def fetch_ranges(
    api: CapitalAPI,
    as_of: datetime,
    on_history: Callable[[str, tuple[Bar, ...], bool], None] | None = None,
    epics: tuple[str, ...] = EPICS,
) -> dict[str, ReferenceRange]:
    """Read exact preceding 20 completed midpoint candles; never include forming bars."""
    minute = as_of.replace(second=0, microsecond=0)
    required = {minute - timedelta(minutes=i) for i in range(1, 21)}
    ranges: dict[str, ReferenceRange] = {}
    for epic in epics:
        response = await api.raw_get(
            f"/api/v1/prices/{epic}", {"resolution": "MINUTE", "max": "25"}
        )
        response.raise_for_status()
        candles: dict[datetime, tuple[float, float]] = {}
        bars: dict[datetime, Bar] = {}
        for record in response.json().get("prices", []):
            raw_time = record.get("snapshotTimeUTC")
            if not isinstance(raw_time, str):
                continue
            time = timestamp(
                raw_time
                if raw_time.endswith("Z") or "+" in raw_time
                else raw_time + "Z"
            )
            if time not in required:
                continue
            values = []
            for name in ("openPrice", "highPrice", "lowPrice", "closePrice"):
                bid, ask = float(record[name]["bid"]), float(record[name]["ask"])
                if not (math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask):
                    raise ValueError("Invalid reference candle")
                values.append((bid + ask) / 2)
            opening, high, low, close = values
            if not low <= min(opening, close) <= max(opening, close) <= high:
                raise ValueError("Invalid reference OHLC")
            if time in candles and candles[time] != (high, low):
                raise ValueError("Conflicting reference candle")
            candles[time] = high, low
            bars[time] = Bar(time=time, open=opening, high=high, low=low, close=close)
        if on_history is not None:
            on_history(
                epic,
                tuple(bars[key] for key in sorted(bars)),
                candles.keys() == required,
            )
        if candles.keys() == required:
            high = max(value[0] for value in candles.values())
            low = min(value[1] for value in candles.values())
            if high > low:
                ranges[epic] = ReferenceRange(high, low)
    return ranges


def state_payload(state: TriggerState) -> dict[str, object]:
    return asdict(state)

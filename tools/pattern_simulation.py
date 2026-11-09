"""Offline hourly pattern simulation."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

HOUR = timedelta(hours=1)
MARKETS = ("Gold", "Crude Oil Spot")
VARIANTS = ("baseline", "same", "opposite")


@dataclass(frozen=True)
class Bar:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


type Series = dict[datetime, Bar]


def context(first: Series, second: Series, time: datetime) -> str:
    previous = time - HOUR
    if any(t not in series for series in (first, second) for t in (time, previous)):
        return "unavailable"
    product = (first[time].close - first[previous].close) * (
        second[time].close - second[previous].close
    )
    return "same" if product > 0 else "opposite" if product < 0 else "neutral"


def signal(history: list[Bar], bar: Bar) -> int:
    high = max(item.high for item in history)
    low = min(item.low for item in history)
    if not low < bar.close < high or (bar.high > high and bar.low < low):
        return 0
    return -1 if bar.high > high else 1 if bar.low < low else 0


def exit_bar(bar: Bar, direction: int, stop: Decimal, target: Decimal):
    if direction == 1:
        if bar.low <= stop:
            return min(stop, bar.open), "stop"
        if bar.high >= target:
            return target, "target"
    else:
        if bar.high >= stop:
            return max(stop, bar.open), "stop"
        if bar.low <= target:
            return target, "target"
    return bar.close, "timeout"


@dataclass(frozen=True)
class Trade:
    time: datetime
    direction: int
    context: str
    entry: Decimal
    stop: Decimal
    target: Decimal
    exit: Decimal
    exit_time: datetime
    reason: str
    net: Decimal
    doubled: Decimal
    favorable: bool
    candles: tuple[Bar, ...]


def simulate(
    ask: Series,
    bid: Series,
    other: Series,
    variant: str,
    extra: Decimal,
    estimated_spread: Decimal,
    boundaries: set[datetime],
) -> tuple[list[Trade], dict[str, int]]:
    trades: list[Trade] = []
    skipped: dict[str, int] = {}
    blocked_until = datetime.min.replace(tzinfo=next(iter(ask)).tzinfo) if ask else None
    times = sorted(ask)
    for index, time in enumerate(times[20:], 20):
        history = [ask[t] for t in times[index - 20 : index]]
        direction = signal(history, ask[time])
        if not direction:
            continue
        state = context(ask, other, time)
        window = [time + HOUR * offset for offset in range(1, 7)]
        entry_side = ask if direction == 1 or not bid else bid
        exit_side = ask if direction == -1 or not bid else bid
        reason = ""
        if variant != "baseline" and variant != state:
            reason = "context"
        elif blocked_until is not None and time < blocked_until:
            reason = "overlap"
        elif any(window[0] <= boundary < window[-1] + HOUR for boundary in boundaries):
            reason = "boundary"
        elif window[0] not in entry_side:
            reason = "missing entry"
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        entry = entry_side[window[0]].open
        stop = ask[time].low if direction == 1 else ask[time].high
        risk = direction * (entry - stop)
        if risk <= 0:
            skipped["invalid stop"] = skipped.get("invalid stop", 0) + 1
            continue
        target = entry + direction * 2 * risk
        blocked_until = window[-1]
        if any(t not in exit_side or t not in ask for t in window):
            skipped["missing outcome"] = skipped.get("missing outcome", 0) + 1
            continue
        for end in window:
            price, reason = exit_bar(exit_side[end], direction, stop, target)
            if reason != "timeout":
                break
        blocked_until = end
        cost = extra if bid else extra + estimated_spread
        gross = direction * (price - entry)
        spread_stress = Decimal(0)
        if bid:
            if window[0] not in bid or end not in bid:
                skipped["missing spread"] = skipped.get("missing spread", 0) + 1
                continue
            spread_stress = (
                ask[window[0]].open
                - bid[window[0]].open
                + ask[end].close
                - bid[end].close
            ) / 2
        trades.append(
            Trade(
                time,
                direction,
                state,
                entry,
                stop,
                target,
                price,
                end,
                reason,
                (gross - cost) / risk,
                (gross - 2 * cost - spread_stress) / risk,
                direction * (ask[window[-1]].close - entry) > 0,
                (*history, ask[time], *(ask[t] for t in window)),
            )
        )
    return trades, skipped

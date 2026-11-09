"""Closed-candle structure analysis; all levels are conditional, never orders."""

import math
from dataclasses import asdict, dataclass


@dataclass
class Candle:
    time: float
    open: float
    high: float
    low: float
    close: float


@dataclass
class Break:
    index: int
    direction: str
    kind: str
    level: float


@dataclass
class Setup:
    direction: str
    entry: float
    lower: float
    upper: float
    index: int


def analyze(bars: list[Candle]) -> dict:
    """Two-sided 2-bar pivots, close breaks, then an untouched FVG midpoint.

    First break establishes bias (BOS); opposite breaks are CHoCH. A gap
    must form on the break candle or either of the next two candles.
    Any subsequent midpoint touch consumes the setup. No look-ahead pivots.
    """
    if len(bars) < 7:
        raise ValueError("At least 7 completed candles are required")
    for i, bar in enumerate(bars):
        if not all(math.isfinite(v) for v in asdict(bar).values()):
            raise ValueError("Invalid candle values")
        if not (
            bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
        ):
            raise ValueError("Invalid candle range")
        if i and bar.time <= bars[i - 1].time:
            raise ValueError("Candles must have increasing timestamps")
    high: float | None = None
    low: float | None = None
    bias = "neutral"
    events: list[Break] = []
    setup: Setup | None = None
    for i, bar in enumerate(bars):
        if i >= 4:
            pivot = bars[i - 2]
            neighbors = [bars[j] for j in (i - 4, i - 3, i - 1, i)]
            if all(pivot.high > b.high for b in neighbors):
                high = pivot.high
            if all(pivot.low < b.low for b in neighbors):
                low = pivot.low
        direction = (
            "buy"
            if high is not None and bar.close > high
            else ("sell" if low is not None and bar.close < low else "")
        )
        if direction:
            level = high if direction == "buy" else low
            assert level is not None
            events.append(
                Break(
                    i,
                    direction,
                    "CHoCH" if bias not in ("neutral", direction) else "BOS",
                    level,
                )
            )
            bias = direction
            setup = None
            if direction == "buy":
                high = None
            else:
                low = None
        if setup and (
            (setup.direction == "buy" and bar.low <= setup.entry)
            or (setup.direction == "sell" and bar.high >= setup.entry)
        ):
            setup = None
        if i < 2 or not events or i - events[-1].index > 2:
            continue
        first = bars[i - 2]
        if bias == "buy" and bar.low > first.high:
            setup = Setup("buy", (first.high + bar.low) / 2, first.high, bar.low, i)
        elif bias == "sell" and bar.high < first.low:
            setup = Setup("sell", (bar.high + first.low) / 2, bar.high, first.low, i)
    return {
        "bars": [asdict(b) for b in bars],
        "events": [asdict(e) for e in events],
        "bias": bias,
        "setup": asdict(setup) if setup else None,
        "buy_break": high,
        "sell_break": low,
    }

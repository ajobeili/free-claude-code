"""Explainable supplementary checks; no probability or execution decisions."""

import math
from dataclasses import asdict, dataclass

from tools.structure_scan import Candle, Setup


@dataclass
class Context:
    timeframe: str
    bias: str
    stale: bool = False


def assess(
    bars: list[Candle],
    setup: Setup | None,
    forming: list[Candle],
    contexts: list[Context],
    bid: float | None,
    ask: float | None,
    market_status: str,
    stale: bool,
    opposite_break: float | None,
) -> dict:
    checks: list[str] = []
    quote_ok = (
        bid is not None
        and ask is not None
        and all(math.isfinite(v) and v > 0 for v in (bid, ask))
        and ask >= bid
    )
    spread = ask - bid if quote_ok and ask is not None and bid is not None else None
    result = {
        "status": "No setup",
        "checks": checks,
        "contexts": [asdict(c) for c in contexts],
        "bid": bid if quote_ok else None,
        "ask": ask if quote_ok else None,
        "spread": spread,
        "market_status": market_status,
        "stop": None,
        "target": None,
        "reward_risk": None,
    }
    if not setup:
        checks.append("No untouched FVG candidate to assess.")
        return result
    buy = setup.direction == "buy"
    touched = any(
        b.low <= setup.entry if buy else b.high >= setup.entry for b in forming
    )
    beyond = quote_ok and (
        ask <= setup.entry
        if buy and ask is not None
        else bid >= setup.entry
        if bid is not None
        else False
    )
    result["status"] = (
        "Stale" if stale else "Touched" if touched or beyond else "Waiting"
    )
    checks.append(
        "Forming candle reached the entry; this is not proof of an order fill."
        if touched or beyond
        else "Entry not reached in the returned forming candles; scan again for updates."
    )
    if not quote_ok:
        checks.append("Quote unavailable: execution spread cannot be checked.")
        if result["status"] == "Waiting":
            result["status"] = "Unverified"
    checks.append(
        f"Market status: {market_status}. Quotes are a broker snapshot, not a streaming feed."
    )
    if market_status != "TRADEABLE" and result["status"] == "Waiting":
        result["status"] = "Unverified"
    checks.extend(
        f"{context.timeframe}: "
        + (
            "unavailable or stale"
            if context.stale or context.bias == "unavailable"
            else "aligned"
            if context.bias == setup.direction
            else "neutral"
            if context.bias == "neutral"
            else "CONFLICT with entry direction"
        )
        for context in contexts
    )
    if opposite_break is not None and (
        setup.entry <= opposite_break if buy else setup.entry >= opposite_break
    ):
        checks.append(
            f"Structure conflict: a completed close {'below' if buy else 'above'} {opposite_break:g} can reverse bias before the proposed entry. A wick touch alone does not confirm that break."
        )
    # Conservative stop beyond the originating three-candle structure, without an invented buffer.
    origin = bars[max(0, setup.index - 2) : setup.index + 1]
    stop = min(b.low for b in origin) if buy else max(b.high for b in origin)
    # Use any confirmed, still-untouched opposing pivot in the scanned history.
    # Confirmation requires two bars on each side. A later wick consumes a level.
    targets = []
    for i in range(2, len(bars) - 2):
        neighbors = [bars[j] for j in (i - 2, i - 1, i + 1, i + 2)]
        later = [*bars[i + 1 :], *forming]
        if (
            buy
            and bars[i].high > setup.entry
            and all(bars[i].high > b.high for b in neighbors)
            and all(b.high < bars[i].high for b in later)
        ):
            if not quote_ok or ask is None or ask < bars[i].high:
                targets.append(bars[i].high)
        elif (
            not buy
            and bars[i].low < setup.entry
            and all(bars[i].low < b.low for b in neighbors)
            and all(b.low > bars[i].low for b in later)
            and (not quote_ok or bid is None or bid > bars[i].low)
        ):
            targets.append(bars[i].low)
    target = (min(targets) if buy else max(targets)) if targets else None
    invalidated = any(b.low <= stop if buy else b.high >= stop for b in forming)
    invalidated = invalidated or (
        quote_ok
        and (
            bid <= stop
            if buy and bid is not None
            else ask >= stop
            if ask is not None
            else False
        )
    )
    if invalidated and not stale:
        result["status"] = "Invalidated"
        checks.append(
            "A forming candle or current quote crossed the stop reference; reject this candidate."
        )
    prior = bars[max(1, setup.index - 14) : setup.index]
    ranges = [
        max(
            b.high - b.low,
            abs(b.high - bars[max(0, setup.index - len(prior) + j - 1)].close),
            abs(b.low - bars[max(0, setup.index - len(prior) + j - 1)].close),
        )
        for j, b in enumerate(prior)
    ]
    volatility = sum(ranges) / len(ranges) if ranges else 0
    if volatility > 0:
        displacement = bars[setup.index - 1]
        checks.append(
            f"Gap size: {(setup.upper - setup.lower) / volatility:.2f} x recent average true range; middle candle body: {abs(displacement.close - displacement.open) / volatility:.2f} x. Descriptive measures, not validated pass thresholds."
        )
    checks.append(f"Gap age: {len(bars) - 1 - setup.index} completed candles.")
    result["stop"] = stop
    result["target"] = target
    risk = abs(setup.entry - stop)
    if target is not None and spread is not None and risk > 0:
        result["reward_risk"] = max(0, abs(target - setup.entry) - spread) / (
            risk + spread
        )
    checks.append(
        "Stop reference: outer edge of the three candles that formed the gap; slippage can exceed this level."
    )
    if target is None:
        checks.append(
            "Target unavailable: no untouched confirmed opposing pivot ahead of entry. Reward-to-risk withheld."
        )
    checks.append(
        "Reward-to-risk uses a conservative one-spread adjustment; commission and slippage are not included."
    )
    checks.append(
        "News calendar and historical performance are not evaluated. These checks are not a win probability."
    )
    return result

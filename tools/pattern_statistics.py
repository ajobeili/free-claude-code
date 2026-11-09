"""Deterministic offline summaries and paired date-block bounds."""

import math
from datetime import date
from random import Random

from tools.pattern_simulation import Trade


def summarize(trades: list[Trade]) -> dict[str, float | int | None]:
    count = len(trades)
    running = peak = drawdown = 0.0
    for trade in trades:
        running += float(trade.net)
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
    return {
        "trades": count,
        "dates": len({trade.time.date() for trade in trades}),
        "profitable": sum(trade.net > 0 for trade in trades),
        "target_rate": sum(trade.reason == "target" for trade in trades) / count
        if count
        else None,
        "direction_rate": sum(trade.favorable for trade in trades) / count
        if count
        else None,
        "net_R": running,
        "mean_R": running / count if count else None,
        "drawdown_R": drawdown,
        "doubled_cost_R": sum(float(trade.doubled) for trade in trades),
    }


def bounds(groups: list[list[Trade]], dates: list[date], repetitions: int = 50_000):
    if len(dates) < 5:
        return [None] * 10
    positions = {day: index for index, day in enumerate(dates)}
    totals = [[0.0] * len(dates) for _ in groups]
    counts = [[0] * len(dates) for _ in groups]
    for group, trades in enumerate(groups):
        for trade in trades:
            index = positions[trade.time.date()]
            totals[group][index] += float(trade.net)
            counts[group][index] += 1
    samples: list[list[float]] = [[] for _ in range(10)]
    random = Random(204)
    for _ in range(repetitions):
        indices: list[int] = []
        while len(indices) < len(dates):
            start = random.randrange(len(dates) - 4)
            indices.extend(range(start, start + 5))
        indices = indices[: len(dates)]
        means = []
        for total, count in zip(totals, counts, strict=True):
            denominator = sum(count[i] for i in indices)
            means.append(
                sum(total[i] for i in indices) / denominator
                if denominator
                else -math.inf
            )
        contrasts = [
            means[i] - means[i - i % 3]
            if math.isfinite(means[i - i % 3])
            else -math.inf
            for i in (1, 2, 4, 5)
        ]
        for sample, value in zip(samples, means + contrasts, strict=True):
            sample.append(value)
    rank = math.ceil(repetitions * 0.005) - 1
    result = [sorted(sample)[rank] for sample in samples]
    return [value if math.isfinite(value) else None for value in result]

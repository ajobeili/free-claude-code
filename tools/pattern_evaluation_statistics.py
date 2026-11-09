"""Frozen, offline eight-group inference. No trading or notification capability.

The paired moving-block method gives approximate uncertainty bounds under
limited dependence and stability assumptions. Its fixed settings do not prove
profitability, execution feasibility, power, or exact family-wise coverage.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as Date
from functools import lru_cache
from random import Random

from tools.pattern_evaluation_events import MARKETS, VARIANTS, Market, Row, Variant

REPLICATES = 50000
SEED = 204
BLOCK_LENGTH = 5
LOWER_INDEX = 178
GROUP_KEYS = tuple((market, variant) for market in MARKETS for variant in VARIANTS)
ALTERNATIVES = (1, 2, 3, 5, 6, 7)
ASSUMPTIONS = (
    "Approximate paired five-date block uncertainty; 50,000 draws, seed 204, "
    "one-sided alpha 0.05/14. Dependence and stability assumptions apply."
)


def _valid_date(value: str) -> None:
    try:
        parsed = Date.fromisoformat(value)
    except ValueError, TypeError:
        raise ValueError("Observation dates must be ISO UTC return dates") from None
    if parsed.isoformat() != value:
        raise ValueError("Observation dates must be canonical YYYY-MM-DD")


@dataclass(frozen=True)
class Observation:
    """One complete, matched, phase-eligible 180-second quote-side outcome.

    The report owner selects primary outcomes before constructing these records.
    Costs are in underlying price points; spread is already embedded in points.
    """

    market: Market
    variant: Variant
    date: str
    points: float
    entry_spread: float
    exit_spread: float
    nonspread_cost: float

    def __post_init__(self):
        if self.market not in MARKETS or self.variant not in VARIANTS:
            raise ValueError("Unknown market or frozen variant")
        _valid_date(self.date)
        if (
            not all(
                math.isfinite(value)
                for value in (
                    self.points,
                    self.entry_spread,
                    self.exit_spread,
                    self.nonspread_cost,
                )
            )
            or min(self.entry_spread, self.exit_spread, self.nonspread_cost) < 0
        ):
            raise ValueError("Finite points and nonnegative measured costs required")

    @property
    def normal(self) -> float:
        return self.points - self.nonspread_cost

    @property
    def doubled(self) -> float:
        return (
            self.normal
            - (self.entry_spread + self.exit_spread) / 2
            - self.nonspread_cost
        )


@dataclass(frozen=True)
class GateEvidence:
    """Facts derived by the report owner from actual frozen source evidence.

    No browser/CLI boolean can override these prerequisites. In particular,
    period_finalized requires the complete declared period and outcome windows.
    """

    period_finalized: bool
    session_complete: bool
    costs_known: bool
    timing_verified: bool
    history_verified: bool
    parity_verified: bool
    phase_validation: bool

    def missing(self) -> list[str]:
        return [name for name in self.__dataclass_fields__ if not getattr(self, name)]


def lower_bound(values: Sequence[float]) -> float | None:
    """Return exactly the 179th-smallest of 50,000 frozen replicates.

    Undefined/nonfinite draws are negative infinity, not dropped and redrawn.
    """
    if len(values) != REPLICATES:
        raise ValueError("The frozen method requires exactly 50,000 replicates")
    ordered = sorted(value if math.isfinite(value) else -math.inf for value in values)
    result = ordered[LOWER_INDEX]
    return result if math.isfinite(result) else None


@lru_cache(maxsize=8)
def _bounds(
    dates: tuple[str, ...],
    aggregates: tuple[tuple[tuple[float, int], ...], ...],
) -> tuple[float | None, ...]:
    """Cache only immutable aggregate inputs and fourteen scalar results."""
    count = len(dates)
    if count < BLOCK_LENGTH:
        return (None,) * 14
    random = Random(SEED)
    samples: list[list[float]] = [[] for _ in range(14)]
    for _ in range(REPLICATES):
        indices: list[int] = []
        while len(indices) < count:
            start = random.randrange(count - BLOCK_LENGTH + 1)
            indices.extend(range(start, start + BLOCK_LENGTH))
        indices = indices[:count]
        means = []
        for group in aggregates:
            denominator = sum(group[index][1] for index in indices)
            numerator = math.fsum(group[index][0] for index in indices)
            means.append(numerator / denominator if denominator else -math.inf)
        contrasts = []
        for index in ALTERNATIVES:
            baseline = 0 if index < 4 else 4
            contrasts.append(
                means[index] - means[baseline]
                if math.isfinite(means[index]) and math.isfinite(means[baseline])
                else -math.inf
            )
        for values, value in zip(samples, [*means, *contrasts], strict=True):
            values.append(value)
    return tuple(lower_bound(values) for values in samples)


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    result = math.fsum(values) / len(values)
    return result if math.isfinite(result) else None


def assess(
    observations: Sequence[Observation],
    eligible_dates: Sequence[str],
    evidence: GateEvidence,
) -> tuple[list[Row], list[Row], list[Row]]:
    """Assess eight means and six own-market comparisons with frozen gates.

    Eligible dates must include complete zero-match session dates. Observations
    outside that explicit common date set are excluded from formal aggregates;
    their descriptive outcomes remain owned by the report's follow-up records.
    """
    days = tuple(eligible_dates)
    for day in days:
        _valid_date(day)
    if tuple(sorted(set(days))) != days:
        raise ValueError("Eligible dates must be distinct and ordered")
    by_date = set(days)
    grouped: list[list[Observation]] = [
        [
            row
            for row in observations
            if (row.market, row.variant) == key and row.date in by_date
        ]
        for key in GROUP_KEYS
    ]
    daily = tuple(
        tuple(
            (
                math.fsum(row.normal for row in group if row.date == day),
                sum(row.date == day for row in group),
            )
            for day in days
        )
        for group in grouped
    )
    bounds = (
        _bounds(days, daily)
        if observations and len(days) >= BLOCK_LENGTH
        else (None,) * 14
    )
    groups: list[Row] = []
    for index, ((market, variant), group) in enumerate(
        zip(GROUP_KEYS, grouped, strict=True)
    ):
        groups.append(
            {
                "market": market,
                "variant": variant,
                "observations": len(group),
                "return_dates": len({row.date for row in group}),
                "normal_mean": _mean([row.normal for row in group]),
                "doubled_mean": _mean([row.doubled for row in group]),
                "mean_lower": bounds[index],
                "decision": "reference",
                "reason": "Operating-rule reference; no automatic promotion. "
                + ASSUMPTIONS,
            }
        )

    comparisons: list[Row] = []
    for position, index in enumerate(ALTERNATIVES):
        baseline = groups[0 if index < 4 else 4]
        group = groups[index]
        alternative_mean, baseline_mean = group["normal_mean"], baseline["normal_mean"]
        improvement = (
            float(alternative_mean) - float(baseline_mean)
            if isinstance(alternative_mean, int | float)
            and isinstance(baseline_mean, int | float)
            else None
        )
        lower = bounds[8 + position]
        failed = evidence.missing()
        for role, item in (("alternative", group), ("baseline", baseline)):
            observations_count = item["observations"]
            return_dates = item["return_dates"]
            if not isinstance(observations_count, int) or observations_count < 100:
                failed.append(role + "_100_observations")
            if not isinstance(return_dates, int) or return_dates < 60:
                failed.append(role + "_60_return_dates")
        if (
            alternative_mean is None
            or improvement is None
            or group["mean_lower"] is None
            or lower is None
        ):
            failed.append("finite_mean_and_improvement_bounds")
        if failed:
            decision = "insufficient"
            reason = "More verified evidence is required: " + ", ".join(failed) + "."
        else:
            # Narrowing avoids claiming a finite value from missing evidence.
            mean_lower, doubled = group["mean_lower"], group["doubled_mean"]
            assert isinstance(alternative_mean, int | float)
            assert improvement is not None and lower is not None
            assert isinstance(mean_lower, int | float) and isinstance(
                doubled, int | float
            )
            if alternative_mean <= 0:
                failed.append("normal_mean_nonpositive")
            if improvement <= 0:
                failed.append("improvement_nonpositive")
            if doubled <= 0:
                failed.append("doubled_mean_nonpositive")
            if mean_lower <= 0:
                failed.append("mean_lower_nonpositive")
            if lower <= 0:
                failed.append("improvement_lower_nonpositive")
            if alternative_mean <= 0 or improvement <= 0:
                decision = "rejected"
                reason = "Observed primary mean or improvement is nonpositive."
            elif failed:
                decision = "inconclusive"
                reason = "Positive observations do not pass uncertainty or doubled-cost gates."
            else:
                decision = "promising"
                reason = "Promising for further demo research only; no profitable edge or execution guarantee."
        group["decision"], group["reason"] = decision, reason
        comparisons.append(
            {
                "market": group["market"],
                "variant": group["variant"],
                "baseline": "confirmed-90",
                "improvement": improvement,
                "improvement_lower": lower,
                "decision": decision,
                "reason": reason,
                "failed_gates": ", ".join(failed),
            }
        )

    claims: list[Row] = [
        {
            "claim": f"mean:{market}:{variant}",
            "lower": bounds[index],
            "reason": ASSUMPTIONS
            if bounds[index] is not None
            else "Undefined bound; insufficient common-date evidence. " + ASSUMPTIONS,
        }
        for index, (market, variant) in enumerate(GROUP_KEYS)
    ]
    claims.extend(
        {
            "claim": f"improvement:{GROUP_KEYS[index][0]}:{GROUP_KEYS[index][1]}",
            "lower": bounds[8 + position],
            "reason": ASSUMPTIONS
            if bounds[8 + position] is not None
            else "Undefined bound; insufficient common-date evidence. " + ASSUMPTIONS,
        }
        for position, index in enumerate(ALTERNATIVES)
    )
    return groups, comparisons, claims

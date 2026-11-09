"""Frozen eight-group, fourteen-claim research inference and cost gates."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from tools.pattern_evaluation_events import MARKETS, VARIANTS
from tools.pattern_evaluation_statistics import (
    BLOCK_LENGTH,
    LOWER_INDEX,
    REPLICATES,
    SEED,
    GateEvidence,
    Observation,
    assess,
    lower_bound,
)

VERIFIED = GateEvidence(
    period_finalized=True,
    session_complete=True,
    costs_known=True,
    timing_verified=True,
    history_verified=True,
    parity_verified=True,
    phase_validation=True,
)


def dates(count=60):
    return [(date(2026, 1, 1) + timedelta(days=i)).isoformat() for i in range(count)]


def observations(*, count=120, alt_points=4.0, baseline_points=2.0):
    days = dates()
    return [
        Observation(
            market,
            variant,
            days[i % len(days)],
            baseline_points if variant == "confirmed-90" else alt_points,
            0.2,
            0.4,
            0.5,
        )
        for market in MARKETS
        for variant in VARIANTS
        for i in range(count)
    ]


def test_empty_groups_and_all_claim_identities_are_retained():
    groups, comparisons, claims = assess([], [], VERIFIED)
    assert len(groups) == 8 and len(comparisons) == 6 and len(claims) == 14
    assert len({row["claim"] for row in claims}) == 14
    assert all(row["observations"] == 0 for row in groups)
    assert all(row["normal_mean"] is None for row in groups)
    assert all(row["lower"] is None for row in claims)
    assert all(row["decision"] == "insufficient" for row in comparisons)


def test_costs_are_subtracted_once_then_doubled_without_double_counting_spread():
    groups, _, _ = assess(observations(count=1), dates(), VERIFIED)
    baseline = groups[0]
    assert baseline["normal_mean"] == pytest.approx(1.5)
    assert baseline["doubled_mean"] == pytest.approx(0.7)
    assert groups[1]["normal_mean"] == pytest.approx(3.5)
    assert groups[1]["doubled_mean"] == pytest.approx(2.7)


def test_lower_bound_is_the_frozen_179th_order_statistic():
    assert (BLOCK_LENGTH, REPLICATES, SEED, LOWER_INDEX) == (5, 50000, 204, 178)
    assert lower_bound(list(range(50000))) == 178
    assert lower_bound([-float("inf")] * 178 + [1.0] * 49822) == 1
    assert lower_bound([-float("inf")] * 179 + [1.0] * 49821) is None
    with pytest.raises(ValueError):
        lower_bound([1.0] * 49999)


def test_constant_positive_groups_have_six_supported_comparisons():
    groups, comparisons, claims = assess(observations(), dates(), VERIFIED)
    assert len(groups) == 8 and len(claims) == 14
    assert all(row["observations"] == 120 for row in groups)
    assert all(row["return_dates"] == 60 for row in groups)
    assert all(row["improvement"] == pytest.approx(2) for row in comparisons)
    assert all(row["improvement_lower"] == pytest.approx(2) for row in comparisons)
    assert all(row["decision"] == "promising" for row in comparisons)
    assert all("demo" in str(row["reason"]).lower() for row in comparisons)


@pytest.mark.parametrize("missing", GateEvidence.__dataclass_fields__)
def test_each_missing_prerequisite_overrides_numerically_positive_results(missing):
    evidence = replace(VERIFIED, **{missing: False})
    _, comparisons, _ = assess(observations(), dates(), evidence)
    assert all(row["decision"] == "insufficient" for row in comparisons)
    assert all(missing in str(row["failed_gates"]) for row in comparisons)


def test_count_and_return_date_floors_apply_to_both_alternative_and_baseline():
    scarce = observations(count=99)
    _, comparisons, _ = assess(scarce, dates(), VERIFIED)
    assert all(row["decision"] == "insufficient" for row in comparisons)
    repeated_one_date = [replace(row, date=dates()[0]) for row in observations()]
    _, comparisons, _ = assess(repeated_one_date, dates(), VERIFIED)
    assert all(row["decision"] == "insufficient" for row in comparisons)
    no_baseline = [row for row in observations() if row.variant != "confirmed-90"]
    _, comparisons, _ = assess(no_baseline, dates(), VERIFIED)
    assert all(row["decision"] == "insufficient" for row in comparisons)


def test_valid_nonpositive_improvement_rejects_even_with_more_observations():
    groups, comparisons, _ = assess(observations(alt_points=1), dates(), VERIFIED)
    assert groups[1]["normal_mean"] == pytest.approx(0.5)
    assert all(row["improvement"] == pytest.approx(-1) for row in comparisons)
    assert all(row["decision"] == "rejected" for row in comparisons)


def test_positive_normal_mean_with_failed_doubled_cost_gate_is_inconclusive():
    rows = observations(alt_points=0.7, baseline_points=0.6)
    _, comparisons, _ = assess(rows, dates(), VERIFIED)
    assert all(row["improvement"] == pytest.approx(0.1) for row in comparisons)
    assert all(row["decision"] == "inconclusive" for row in comparisons)


def test_five_date_blocks_reweight_by_observations_and_keep_empty_other_groups():
    days = dates(5)
    rows = [Observation("GOLD", "confirmed-90", days[0], 10, 0, 0, 0)]
    rows.extend(
        Observation("GOLD", "confirmed-90", days[1], 0, 0, 0, 0) for _ in range(9)
    )
    rows.extend(Observation("GOLD", "unfiltered-90", day, 2, 0, 0, 0) for day in days)
    groups, comparisons, claims = assess(rows, days, VERIFIED)
    assert groups[0]["normal_mean"] == pytest.approx(1)
    assert groups[0]["mean_lower"] == pytest.approx(1)
    assert comparisons[0]["improvement_lower"] == pytest.approx(1)
    assert len(claims) == 14
    assert sum(row["lower"] is None for row in claims) == 11


def test_variable_shared_shocks_cancel_in_each_paired_contrast():
    days = dates()
    rows = [
        Observation(
            market,
            variant,
            day,
            float(index + 1) + (0 if variant == "confirmed-90" else 2),
            0,
            0,
            0,
        )
        for market in MARKETS
        for variant in VARIANTS
        for index, day in enumerate(days)
        for _ in range(2)
    ]
    groups, comparisons, _ = assess(rows, days, VERIFIED)
    lower, mean = groups[0]["mean_lower"], groups[0]["normal_mean"]
    assert isinstance(lower, float) and isinstance(mean, float)
    assert lower < mean
    assert all(row["improvement"] == pytest.approx(2) for row in comparisons)
    assert all(row["improvement_lower"] == pytest.approx(2) for row in comparisons)


@pytest.mark.parametrize(
    "changes",
    [
        {"points": float("nan")},
        {"points": float("inf")},
        {"entry_spread": -1},
        {"exit_spread": -1},
        {"nonspread_cost": -1},
        {"date": "not-a-date"},
        {"market": "SILVER"},
        {"variant": "unknown"},
    ],
)
def test_invalid_observations_are_rejected(changes):
    row = Observation("GOLD", "confirmed-90", dates()[0], 1, 0.1, 0.1, 0)
    with pytest.raises(ValueError):
        replace(row, **changes)


def test_date_set_requires_distinct_ordered_iso_dates():
    with pytest.raises(ValueError):
        assess([], ["2026-01-02", "2026-01-01"], VERIFIED)
    with pytest.raises(ValueError):
        assess([], ["2026-01-01", "2026-01-01"], VERIFIED)

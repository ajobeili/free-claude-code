"""Exposure denominators include pending/cooldown but never unknown history."""

import asyncio

from tests.test_pattern_evaluation import CaptureHarness


def test_coverage_is_union_of_known_endpoint_intervals_including_cooldown():
    async def run():
        capture = CaptureHarness()
        await capture.prepared()
        await capture.tick("GOLD", 114, 3)
        await capture.tick("OIL_CRUDE", 49, 4)
        await capture.tick("GOLD", 109, 5)
        await capture.tick("GOLD", 108, 6)
        evaluator = capture.assert_parity()
        coverage = [
            row
            for row in evaluator.records()["coverage"]
            if row["market"] == "GOLD" and not row["paired"]
        ]
        assert sum(row["seconds"] for row in coverage) == 4
        assert len(coverage) == 1
        # Same accepted endpoints must not count twice; duplicate target is rejected.
        await capture.tick("GOLD", 108, 6)
        assert (
            capture.assert_parity().records()["coverage"]
            == evaluator.records()["coverage"]
        )

    asyncio.run(run())


def test_unknown_history_and_long_gaps_do_not_create_exposure():
    async def run():
        capture = CaptureHarness(history_markets=())
        await capture.prepared()
        await capture.tick("GOLD", 100, 8)
        evaluator = capture.assert_parity()
        assert evaluator.records()["coverage"] == []
        assert any(row["state"] == "unknown" for row in evaluator.records()["stages"])

    asyncio.run(run())

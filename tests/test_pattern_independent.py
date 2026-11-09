"""Each market qualifies on its own chart, without the other market's data."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_pattern_evaluation import CaptureHarness
from tools.pattern_evaluation import Evaluator
from tools.pattern_evaluation_events import ControlEvidence
from tools.pattern_events import DecisionRecord
from tools.pattern_notifications import format_alert
from tools.pattern_trigger import PatternSettings, PatternTrigger, ReferenceRange

START = datetime(2026, 11, 5, 7, 10, 10, tzinfo=UTC)


def test_telegram_stop_uses_worst_observed_excursion_before_reentry():
    async def run():
        async def history(_):
            return {}

        async def own_history(epic, _):
            return {epic: ReferenceRange(110, 90)}

        trigger = PatternTrigger(
            history, PatternSettings(independent=True), market_history=own_history
        )
        await trigger.feed_status(True)
        for mid, second in ((100, 0), (100, 1), (86, 2), (84, 3), (94, 4)):
            await tick(trigger, "GOLD", mid, second)
        event = trigger.snapshot().alerts[0]
        assert event.breakout_price == 86
        assert event.breakout_extreme == 84
        message = format_alert(event)
        assert "BUY" in message
        assert "Entry: 94.5" in message
        assert "Stop loss: 83.5" in message
        assert "Take profit: 116.5 (2R)" in message

    asyncio.run(run())


def test_stale_target_finishes_only_its_own_recorded_attempt():
    async def run():
        records = []

        async def history(_):
            return {}

        async def own_history(epic, _):
            return {epic: ReferenceRange(110, 90)}

        trigger = PatternTrigger(
            history,
            PatternSettings(independent=True),
            on_record=records.append,
            market_history=own_history,
        )
        await trigger.feed_status(True)
        for market in ("GOLD", "OIL_CRUDE"):
            for mid, second in ((100, 0), (100, 1), (114, 2)):
                await tick(trigger, market, mid, second)
        await tick(trigger, "GOLD", 114, 3, age=31)
        await tick(trigger, "OIL_CRUDE", 109, 4)
        outcomes = [
            (item.epic, item.outcome)
            for item in records
            if isinstance(item, DecisionRecord)
        ]
        assert outcomes == [("GOLD", "interrupted"), ("OIL_CRUDE", "qualified")]

    asyncio.run(run())


async def tick(trigger, market, mid, seconds, age=0):
    time = START + timedelta(seconds=seconds)
    await trigger.on_quote(
        market, time, time + timedelta(seconds=age), mid - 0.5, mid + 0.5
    )


@pytest.mark.parametrize("market", ["GOLD", "OIL_CRUDE"])
def test_one_market_alone_can_start_match_and_alert(market):
    async def run():
        requests = []

        async def history(_):
            raise AssertionError("Combined history must not be requested")

        async def own_history(epic, _):
            requests.append(epic)
            return {epic: ReferenceRange(110, 90)}

        trigger = PatternTrigger(
            history, PatternSettings(independent=True), market_history=own_history
        )
        await trigger.feed_status(True)
        for mid, second in ((100, 0), (100, 1), (114, 2), (109, 3)):
            await tick(trigger, market, mid, second)
        alerts = trigger.snapshot().alerts
        assert requests == [market]
        assert len(alerts) == 1
        assert alerts[0].epic == market
        assert alerts[0].other_market == ""
        assert alerts[0].rule_version == "independent-rejection-v2"

    asyncio.run(run())


def test_other_market_history_failure_stale_quote_and_new_minute_do_not_cancel_gold():
    async def run():
        async def history(_):
            return {}

        async def own_history(epic, _):
            if epic == "OIL_CRUDE":
                raise OSError("Oil history unavailable")
            return {epic: ReferenceRange(110, 90)}

        trigger = PatternTrigger(
            history, PatternSettings(independent=True), market_history=own_history
        )
        await trigger.feed_status(True)
        await tick(trigger, "GOLD", 100, 0)
        await tick(trigger, "GOLD", 100, 1)
        await tick(trigger, "GOLD", 114, 2)
        await tick(trigger, "OIL_CRUDE", 50, 3, age=31)
        await tick(trigger, "OIL_CRUDE", 50, 51)
        await tick(trigger, "GOLD", 109, 52)
        assert len(trigger.snapshot().alerts) == 1
        assert trigger.snapshot().alerts[0].epic == "GOLD"

    asyncio.run(run())


def test_other_market_later_source_time_does_not_expire_target_attempt():
    async def run():
        async def history(_):
            return {}

        async def own_history(epic, _):
            return {epic: ReferenceRange(110, 90)}

        trigger = PatternTrigger(
            history, PatternSettings(independent=True), market_history=own_history
        )
        await trigger.feed_status(True)
        await tick(trigger, "GOLD", 100, 0)
        await tick(trigger, "GOLD", 100, 1)
        await tick(trigger, "GOLD", 114, 2)
        await tick(trigger, "OIL_CRUDE", 100, 95)
        await tick(trigger, "GOLD", 109, 92)
        assert len(trigger.snapshot().alerts) == 1

    asyncio.run(run())


def test_independent_capture_replay_matches_actual_detector_without_oil():
    async def run():
        capture = CaptureHarness(history_markets=("GOLD",))

        async def own_history(_epic, time):
            return await capture.history(time)

        capture.trigger = PatternTrigger(
            capture.history,
            PatternSettings(independent=True),
            on_record=capture.record,
            on_alert=capture.alerts.append,
            market_history=own_history,
        )
        capture.emit("control", ControlEvidence(reason="operating_independent_v2"))
        await capture.connected(True)
        for mid, second in ((100, 0), (100, 1), (114, 2), (109, 3)):
            await capture.tick("GOLD", mid, second)
        replay = Evaluator(("confirmed-90",))
        for event in capture.events:
            replay.process(event)
        assert replay.summary()["independent"] is True
        assert replay.parity() == "verified"
        assert replay.records()["attempts"][0]["outcome"] == "matched"
        assert len(capture.alerts) == 1

    asyncio.run(run())

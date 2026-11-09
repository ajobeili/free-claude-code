"""Live breakout/rejection contracts, without broker access."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from tools.pattern_trigger import PatternTrigger, ReferenceRange

START = datetime(2026, 11, 4, 9, 10, 10, tzinfo=UTC)


async def history(as_of):
    return {"GOLD": ReferenceRange(110, 90), "OIL_CRUDE": ReferenceRange(60, 40)}


async def tick(trigger, epic, mid, second, *, spread=1, age=0):
    time = START + timedelta(seconds=second)
    await trigger.on_quote(
        epic, time, time + timedelta(seconds=age), mid - spread / 2, mid + spread / 2
    )


async def prepared(on_alert=None, on_record=None):
    trigger = PatternTrigger(history, on_alert=on_alert, on_record=on_record)
    await trigger.feed_status(True)
    await tick(trigger, "GOLD", 100, 0)
    await tick(trigger, "OIL_CRUDE", 50, 1)
    await tick(trigger, "GOLD", 100, 2)
    return trigger


@pytest.mark.parametrize(
    "attempt,returned,other,direction",
    [
        (114, 109, 49, "DOWN"),
        (86, 91, 51, "UP"),
    ],
)
def test_live_reentry_fires_once_without_candle_close(
    attempt, returned, other, direction
):
    async def run():
        trigger = await prepared()
        await tick(trigger, "GOLD", attempt, 3)
        assert trigger.snapshot().markets[0].phase == "breakout"
        await tick(trigger, "OIL_CRUDE", other, 4)
        await tick(trigger, "GOLD", returned, 5)
        await tick(trigger, "GOLD", returned, 6)
        alerts = trigger.snapshot().alerts
        assert len(alerts) == 1
        assert alerts[0].direction == direction
        assert alerts[0].trigger_time.endswith("09:10:15+00:00")
        assert alerts[0].range_high == 110
        assert alerts[0].other_end == other

    asyncio.run(run())


@pytest.mark.parametrize("other,delay", [(51, 5), (49, 95)])
def test_same_direction_or_late_rejection_does_not_alert(other, delay):
    async def run():
        trigger = await prepared()
        await tick(trigger, "GOLD", 114, 3)
        await tick(trigger, "OIL_CRUDE", other, delay - 1)
        await tick(trigger, "GOLD", 109, delay)
        assert trigger.snapshot().alerts == []

    asyncio.run(run())


def test_spread_noise_and_candle_boundary_never_trigger():
    async def run():
        trigger = await prepared()
        await tick(trigger, "GOLD", 111, 3)
        await tick(trigger, "OIL_CRUDE", 49, 4)
        await tick(trigger, "GOLD", 109, 5)
        await tick(trigger, "OIL_CRUDE", 49, 60)
        await tick(trigger, "GOLD", 109, 61)
        assert trigger.snapshot().alerts == []

    asyncio.run(run())


def test_stale_other_market_and_out_of_order_quotes_cannot_confirm():
    async def run():
        trigger = await prepared()
        await tick(trigger, "GOLD", 114, 3)
        await tick(trigger, "OIL_CRUDE", 49, 4, age=20)
        await tick(trigger, "GOLD", 109, 12)
        await tick(trigger, "GOLD", 114, 2)
        assert trigger.snapshot().alerts == []

    asyncio.run(run())


def test_disconnect_cancels_pending_breakout():
    async def run():
        trigger = await prepared()
        await tick(trigger, "GOLD", 114, 3)
        await trigger.feed_status(False)
        await trigger.feed_status(True)
        await tick(trigger, "OIL_CRUDE", 49, 4)
        await tick(trigger, "GOLD", 109, 5)
        assert trigger.snapshot().alerts == []

    asyncio.run(run())


def test_failed_history_retries_only_on_quotes_and_is_bounded():
    calls = []

    async def unavailable(as_of):
        calls.append(as_of)
        raise RuntimeError("offline")

    async def run():
        trigger = PatternTrigger(unavailable)
        await trigger.feed_status(True)
        for second in range(10):
            await tick(trigger, "GOLD", 100, second)
        assert trigger.snapshot().status == "warming"
        assert trigger.snapshot().alerts == []

    asyncio.run(run())
    assert len(calls) == 3


def test_invalid_quotes_do_not_change_state():
    async def run():
        trigger = await prepared()
        version = trigger.snapshot().version
        await tick(trigger, "GOLD", float("nan"), 3)
        await tick(trigger, "GOLD", 100, 3, spread=-1)
        await tick(trigger, "GOLD", 100, 3, age=-10)
        assert trigger.snapshot().version == version

    asyncio.run(run())


def test_reentry_on_first_quote_of_new_minute_uses_frozen_range():
    async def run():
        trigger = await prepared()
        await tick(trigger, "OIL_CRUDE", 50, 46)
        await tick(trigger, "GOLD", 114, 47)
        await tick(trigger, "OIL_CRUDE", 49, 49)
        await tick(trigger, "GOLD", 109, 50)
        assert len(trigger.snapshot().alerts) == 1

    asyncio.run(run())


def test_oil_can_trigger_using_gold_divergence():
    async def run():
        trigger = await prepared()
        await tick(trigger, "OIL_CRUDE", 64, 3)
        await tick(trigger, "GOLD", 99, 4)
        await tick(trigger, "OIL_CRUDE", 59, 5)
        assert trigger.snapshot().alerts[0].epic == "OIL_CRUDE"

    asyncio.run(run())


def test_old_timestamps_pause_alerts_and_cancel_pending_attempts():
    async def run():
        trigger = await prepared()
        await tick(trigger, "GOLD", 114, 3)
        await tick(trigger, "OIL_CRUDE", 49, 4, age=40)
        assert trigger.snapshot().status == "delayed"
        assert "Alerts paused" in trigger.snapshot().error
        await tick(trigger, "GOLD", 109, 5)
        assert trigger.snapshot().alerts == []

    asyncio.run(run())


def test_delayed_research_alert_is_explicitly_labelled_and_retains_event_time():
    async def run():
        trigger = await prepared()
        await tick(trigger, "GOLD", 114, 3, age=15)
        await tick(trigger, "OIL_CRUDE", 49, 4, age=15)
        await tick(trigger, "GOLD", 109, 5, age=15)
        alert = trigger.snapshot().alerts[0]
        assert alert.data_mode == "DELAYED RESEARCH"
        assert alert.quote_delay_seconds == 16
        assert alert.trigger_time.endswith("09:10:15+00:00")
        assert alert.observed_time.endswith("09:10:30+00:00")
        assert trigger.snapshot().status == "watching"
        assert trigger.snapshot().quote_delay_seconds >= 15

    asyncio.run(run())


@pytest.mark.parametrize("age,paused", [(30, False), (30.01, True)])
def test_research_delay_has_a_hard_upper_bound(age, paused):
    async def run():
        trigger = await prepared()
        await tick(trigger, "GOLD", 114, 3, age=age)
        assert (trigger.snapshot().status == "delayed") is paused

    asyncio.run(run())


def test_only_a_new_qualifying_pattern_enqueues_a_notification():
    alerts = []

    async def run():
        trigger = await prepared(alerts.append)
        assert alerts == []
        await tick(trigger, "GOLD", 114, 3)
        assert alerts == []
        await tick(trigger, "OIL_CRUDE", 49, 4)
        await tick(trigger, "GOLD", 109, 5)
        await tick(trigger, "GOLD", 109, 6)
        assert len(alerts) == 1
        assert alerts[0] == trigger.snapshot().alerts[0]

    asyncio.run(run())


def test_journal_records_raw_quotes_attempt_and_failed_context_without_alert():
    from tools.pattern_events import AttemptRecord, DecisionRecord, QuoteRecord

    records = []

    async def run():
        trigger = await prepared(on_record=records.append)
        await tick(trigger, "GOLD", 114, 3)
        await tick(trigger, "OIL_CRUDE", 51, 4)
        await tick(trigger, "GOLD", 109, 5)
        assert trigger.snapshot().alerts == []

    asyncio.run(run())
    assert any(isinstance(item, QuoteRecord) and item.bid == 113.5 for item in records)
    assert sum(isinstance(item, AttemptRecord) for item in records) == 1
    decisions = [item for item in records if isinstance(item, DecisionRecord)]
    assert len(decisions) == 1
    assert decisions[0].outcome == "context_failed"
    assert "did not move oppositely" in decisions[0].reason


def test_journal_records_expired_breakout():
    from tools.pattern_events import DecisionRecord

    records = []

    async def run():
        trigger = await prepared(on_record=records.append)
        await tick(trigger, "GOLD", 114, 3)
        await tick(trigger, "GOLD", 109, 95)

    asyncio.run(run())
    assert any(
        isinstance(item, DecisionRecord) and item.outcome == "expired"
        for item in records
    )


def test_journaling_does_not_change_baseline_signals():
    records = []

    async def run():
        plain = await prepared()
        recorded = await prepared(on_record=records.append)
        for trigger in (plain, recorded):
            await tick(trigger, "GOLD", 114, 3)
            await tick(trigger, "OIL_CRUDE", 49, 4)
            await tick(trigger, "GOLD", 109, 5)
        assert plain.snapshot() == recorded.snapshot()
        assert records

    asyncio.run(run())


def test_pattern_module_starts_only_quote_stream(monkeypatch):
    from tools import scan_server

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(scan_server, "_ACTIVE_MODULE", "home")
    monkeypatch.setattr(scan_server, "_MODULE_LOCK", asyncio.Lock())
    monkeypatch.setattr(scan_server, "_LIVE_ENGINE_TASK", None)
    monkeypatch.setattr(scan_server, "_CAPITAL_STREAM_TASK", None)
    monkeypatch.setattr(scan_server, "_capital_stream_loop", idle)

    async def run():
        await scan_server._activate_module("pattern")
        stream = scan_server._CAPITAL_STREAM_TASK
        assert stream is not None
        assert scan_server._LIVE_ENGINE_TASK is None
        await scan_server._activate_module("home")
        assert stream.cancelled()

    asyncio.run(run())


def test_millisecond_timestamp_and_bid_offer_reach_detector(monkeypatch):
    from tools import scan_server

    captured = []

    class Trigger:
        async def on_quote(self, *args):
            captured.append(args)

    monkeypatch.setattr(scan_server, "_ACTIVE_MODULE", "pattern")
    monkeypatch.setattr(scan_server, "_PATTERN_TRIGGER", Trigger())
    asyncio.run(
        scan_server._pattern_quote(
            "GOLD", START.timestamp() * 1000, START.timestamp(), 99, 101
        )
    )
    assert captured == [("GOLD", START, START, 99, 101)]

"""Notification contracts: no real delivery during tests."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tools.pattern_notifications import PatternNotifier, format_alert, send_via_hermes
from tools.pattern_trigger import PatternAlert


def alert():
    return PatternAlert(
        "GOLD",
        "Gold",
        "DOWN",
        "2026-11-04T17:10:05Z",
        "2026-11-04T17:10:01Z",
        110,
        90,
        114,
        109,
        1,
        "Crude oil",
        50,
        49,
        data_mode="DELAYED RESEARCH",
        quote_delay_seconds=13,
        observed_time="2026-11-04T17:10:18Z",
    )


def test_message_is_brief_and_keeps_delay_and_no_trade_statement():
    message = format_alert(alert())
    assert "Delay 13.0s" in message
    assert "No trade placed" in message
    assert len(message.splitlines()) <= 7
    assert len(message) <= 320
    assert "Pattern time" not in message
    assert "Breakout:" not in message


@pytest.mark.parametrize(
    "direction,expected",
    [
        (
            "UP",
            (
                "BUY",
                "Entry: 109.5",
                "Stop loss: 84.5",
                "Take profit: 159.5 (2R)",
            ),
        ),
        (
            "DOWN",
            (
                "SELL",
                "Entry: 108.5",
                "Stop loss: 115.5",
                "Take profit: 94.5 (2R)",
            ),
        ),
    ],
)
def test_demo_message_has_explicit_direction_quote_sides_stop_and_target(
    direction, expected
):
    event = replace(
        alert(),
        direction=direction,
        rule_version="independent-rejection-v2",
        other_market="",
        other_start=None,
        other_end=None,
        breakout_extreme=85 if direction == "UP" else 115,
        quote_delay_seconds=1,
        data_mode="RESEARCH",
    )
    message = format_alert(event)
    for value in expected:
        assert value in message
    assert "DEMO" in message
    assert "No trade placed" in message
    assert "opposite" not in message


def test_delayed_prices_and_invalid_levels_never_look_like_current_entry():
    delayed = replace(
        alert(), rule_version="independent-rejection-v2", breakout_extreme=115
    )
    message = format_alert(delayed)
    assert "SELL" in message and "WAIT — delayed quote" in message
    invalid = replace(delayed, breakout_extreme=100)
    invalid_message = format_alert(invalid)
    assert "WAIT — invalid price levels" in invalid_message
    assert "Stop loss: unavailable" in invalid_message
    assert "Take profit: unavailable (2R)" in invalid_message


@pytest.mark.parametrize("sent", [True, False])
def test_each_new_event_is_sent_once_and_delivery_outcome_is_reported(sent):
    calls = []

    async def sender(message):
        calls.append(message)
        return sent

    async def run():
        notifier = PatternNotifier(True, "hermes", "telegram:test", sender)
        worker = asyncio.create_task(notifier.run())
        fresh = replace(alert(), quote_delay_seconds=1, data_mode="RESEARCH")
        notifier.enqueue(fresh)
        notifier.enqueue(fresh)
        await asyncio.wait_for(notifier.drain(), 1)
        assert len(calls) == 1
        assert notifier.snapshot()["sent"] == int(sent)
        assert notifier.snapshot()["failed"] == int(not sent)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())


def test_disabled_notifications_never_queue_or_send():
    async def unexpected(message):
        raise AssertionError("Unexpected send")

    notifier = PatternNotifier(False, "hermes", "telegram:test", unexpected)
    notifier.enqueue(alert())
    assert notifier.snapshot()["pending"] == 0


def test_full_queue_is_bounded_and_reported():
    from dataclasses import replace

    notifier = PatternNotifier(True, "hermes", "telegram:test")
    for number in range(21):
        notifier.enqueue(replace(alert(), trigger_time=str(number)))
    assert notifier.snapshot()["pending"] == 20
    assert notifier.snapshot()["failed"] == 1


def test_expired_queued_event_is_not_sent_late(monkeypatch):
    clock = iter([0, 61])
    monkeypatch.setattr(
        "tools.pattern_notifications.time",
        SimpleNamespace(monotonic=lambda: next(clock)),
    )

    async def unexpected(message):
        raise AssertionError("Expired alert was sent")

    async def run():
        notifier = PatternNotifier(True, "hermes", "telegram:test", unexpected)
        notifier.enqueue(alert())
        worker = asyncio.create_task(notifier.run())
        await asyncio.wait_for(notifier.drain(), 1)
        assert notifier.snapshot()["stale"] == 1
        assert notifier.snapshot()["failed"] == 0
        assert notifier.snapshot()["sent"] == 0
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize(
    "output,expected",
    [
        (b'{"success":true}', True),
        (b'{"error":"failed"}', False),
        (b'{"skipped":true}', False),
        (b"not-json", False),
    ],
)
def test_hermes_delivery_uses_stdin_and_requires_explicit_success(
    monkeypatch, output, expected
):
    class Process:
        returncode = 0

        async def communicate(self, data):
            assert data == b"test only"
            return output, b""

    async def spawn(*args, **kwargs):
        assert args == (
            "hermes",
            "send",
            "--to",
            "telegram:test",
            "--file",
            "-",
            "--json",
        )
        assert "shell" not in kwargs
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert (
        asyncio.run(send_via_hermes("hermes", "telegram:test", "test only")) is expected
    )


@pytest.mark.parametrize("startup_seconds,expected", [(53, True), (121, False)])
def test_delivery_accepts_slow_cold_start_but_still_bounds_wait(
    monkeypatch, startup_seconds, expected
):
    """Advance a dedicated loop's clock; no real waiting or Telegram delivery."""

    async def run():
        loop = asyncio.get_running_loop()
        original_time = loop.time
        elapsed = [0.0]
        loop.time = lambda: original_time() + elapsed[0]

        class Process:
            returncode = 0

            def kill(self):
                self.returncode = -9

            async def communicate(self, data=None):
                if data is not None:
                    elapsed[0] += startup_seconds
                    # Run the ready queue and expired timer before completing.
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                return b'{"success":true}', b""

        async def spawn(*args, **kwargs):
            return Process()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        try:
            result = await send_via_hermes("hermes", "telegram:test", "test only")
            assert result is expected
        finally:
            loop.time = original_time

    asyncio.run(run())

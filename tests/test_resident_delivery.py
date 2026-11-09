"""Exercise resident request handling with fake Telegram transport only."""

import ast
import asyncio
import json
import runpy
import time
from dataclasses import replace
from pathlib import Path

import pytest

from tools.pattern_notifications import PatternNotifier
from tools.pattern_trigger import PatternAlert

DeliveryServer = runpy.run_path(
    str(Path(__file__).parents[1] / "deploy/hermes_delivery_worker.py")
)["DeliveryServer"]


def test_worker_syntax_supports_the_hermes_python_311_runtime():
    source = Path(__file__).parents[1] / "deploy/hermes_delivery_worker.py"
    ast.parse(source.read_text(encoding="utf-8"), feature_version=(3, 11))


class Writer:
    def __init__(self):
        self.output = b""
        self.closed = False

    def write(self, data):
        self.output += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


async def request(server, payload):
    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps(payload) + "\n").encode())
    reader.feed_eof()
    writer = Writer()
    await server.handle(reader, writer)
    assert writer.closed
    return json.loads(writer.output)


def test_health_and_multiple_fresh_requests_reuse_one_sender():
    calls = []

    def send(target, message):
        calls.append((target, message))
        return '{"success":true}'

    async def run():
        server = DeliveryServer("telegram:test", send)
        assert await request(server, {"op": "health"}) == {"ready": True}
        assert calls == []
        for message in ("first", "second"):
            result = await request(
                server,
                {
                    "target": "telegram:test",
                    "message": message,
                    "deadline": time.monotonic() + 4,
                },
            )
            assert result["success"]
        assert calls == [("telegram:test", "first"), ("telegram:test", "second")]

    asyncio.run(run())


@pytest.mark.parametrize("target,age", [("telegram:other", -1), ("telegram:test", 1)])
def test_wrong_destination_and_expired_requests_never_send(target, age):
    def unexpected(*args):
        raise AssertionError("No Telegram delivery is permitted")

    async def run():
        server = DeliveryServer("telegram:test", unexpected)
        result = await request(
            server,
            {
                "target": target,
                "message": "expired",
                "deadline": time.monotonic() - age,
            },
        )
        assert result["success"] is False

    asyncio.run(run())


def test_request_that_expires_while_waiting_for_sender_never_sends():
    def unexpected(*args):
        raise AssertionError("Queued stale alert reached Telegram")

    async def run():
        server = DeliveryServer("telegram:test", unexpected)
        await server.lock.acquire()
        task = asyncio.create_task(
            request(
                server,
                {
                    "target": "telegram:test",
                    "message": "queued",
                    "deadline": time.monotonic() + 0.01,
                },
            )
        )
        await asyncio.sleep(0.03)
        server.lock.release()
        assert (await task)["reason"] == "expired"

    asyncio.run(run())


def fresh_alert():
    return PatternAlert(
        "GOLD",
        "Gold",
        "DOWN",
        "2026-11-09T17:00:00Z",
        "2026-11-09T16:59:50Z",
        110,
        90,
        114,
        109,
        1,
        "",
        None,
        None,
        quote_delay_seconds=1,
        breakout_extreme=115,
    )


@pytest.mark.parametrize("delay", [5, 15, float("nan")])
def test_stale_quotes_are_counted_without_delivery(delay):
    async def unexpected(message):
        raise AssertionError("Stale quote should remain research history only")

    async def run():
        notifier = PatternNotifier(True, "hermes", "telegram:test", unexpected)
        worker = asyncio.create_task(notifier.run())
        notifier.enqueue(replace(fresh_alert(), quote_delay_seconds=delay))
        await asyncio.wait_for(notifier.drain(), 1)
        assert notifier.snapshot()["stale"] == 1
        assert notifier.snapshot()["sent"] == 0
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())


def test_fresh_alert_uses_resident_sender_with_expiry_and_remaining_budget(monkeypatch):
    calls = []

    async def ready(path):
        return True

    async def send(path, target, message, deadline):
        assert 0 < deadline - time.monotonic() <= 4
        assert "expires" in message
        calls.append(message)
        return True

    monkeypatch.setattr("tools.pattern_notifications.resident_ready", ready)
    monkeypatch.setattr("tools.pattern_notifications.send_resident", send)

    async def run():
        notifier = PatternNotifier(
            True, "hermes", "telegram:test", socket_path="/private/test.sock"
        )
        worker = asyncio.create_task(notifier.run())
        notifier.enqueue(fresh_alert())
        await asyncio.wait_for(notifier.drain(), 1)
        assert len(calls) == 1
        assert notifier.snapshot()["sent"] == 1
        assert notifier.snapshot()["transport"] == "resident Hermes"
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())

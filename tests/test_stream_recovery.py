"""Broker keepalive, quote health, and reconnect contracts."""

import asyncio
import json
from types import SimpleNamespace

import aiohttp
import httpx
import pytest

from tools import scan_server
from tools.capital_api import CapitalAPI


def test_keepalive_uses_broker_ping_protocol(monkeypatch):
    sent = []
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    class Socket:
        async def send_json(self, message):
            sent.append(message)
            raise asyncio.CancelledError

    monkeypatch.setattr(scan_server.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scan_server._capital_keepalive(Socket(), "cst", "token"))
    assert waits == [240]
    assert sent == [
        {
            "destination": "ping",
            "correlationId": "fcc-keepalive",
            "cst": "cst",
            "securityToken": "token",
        }
    ]


@pytest.mark.parametrize("round_trip,valid", [(0.2, True), (3, False)])
def test_quote_clock_calibration_uses_broker_time(monkeypatch, round_trip, valid):
    api = CapitalAPI()
    clock = iter([200, 200 + round_trip])
    monkeypatch.setattr(
        "tools.capital_api.time", SimpleNamespace(time=lambda: next(clock))
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"serverTime": 170000})
            )
        ) as client:
            api._client = client
            return await api.clock_offset()

    if valid:
        assert asyncio.run(run()) == pytest.approx(30.1)
    else:
        with pytest.raises(RuntimeError, match="clock"):
            asyncio.run(run())


@pytest.mark.parametrize("rejected", [False, True])
def test_stream_only_reports_connected_after_both_quotes_and_clears_on_close(
    monkeypatch, rejected
):
    feeds = []
    received = []
    checks = []
    tasks = []
    connections = []

    class API:
        cst = "cst"
        x_security = "token"

        async def ensure_session(self):
            checks.append(True)

        async def clock_offset(self):
            return 0

    class Trigger:
        async def feed_status(self, connected):
            feeds.append(connected)

    class Socket:
        async def send_json(self, message):
            assert message["destination"] == "marketData.subscribe"
            assert True not in feeds

        def __aiter__(self):
            return self.frames()

        async def frames(self):
            yield SimpleNamespace(
                type=aiohttp.WSMsgType.TEXT,
                data=json.dumps(
                    {
                        "destination": "marketData.subscribe",
                        "status": "ERROR" if rejected else "OK",
                    }
                ),
            )
            assert True not in feeds
            if rejected:
                pytest.fail("Rejected subscription was silently ignored")
            for epic, bid, offer in [
                ("GOLD", 2, 1),
                ("GOLD", 1, 2),
                ("OIL_CRUDE", 1, 2),
            ]:
                yield SimpleNamespace(
                    type=aiohttp.WSMsgType.TEXT,
                    data=json.dumps(
                        {
                            "destination": "quote",
                            "payload": {
                                "epic": epic,
                                "bid": bid,
                                "ofr": offer,
                                "timestamp": 1793790000000,
                            },
                        }
                    ),
                )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            if connections:
                raise asyncio.CancelledError
            connections.append(True)
            return self

        async def __aexit__(self, *args):
            return None

        def ws_connect(self, *args, **kwargs):
            return Socket()

    async def keepalive(*args):
        tasks.append(asyncio.current_task())
        await asyncio.Event().wait()

    async def quote(epic, raw_time, received_time, bid, offer):
        received.append(epic)

    async def stop_retry(seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(scan_server, "API", API())
    monkeypatch.setattr(scan_server, "_ACTIVE_MODULE", "pattern")
    monkeypatch.setattr(scan_server, "_STREAM_STATUS", {})
    monkeypatch.setattr(scan_server, "_TICK_CACHE", {})
    monkeypatch.setattr(scan_server, "_PATTERN_TRIGGER", Trigger())
    monkeypatch.setattr(scan_server, "_pattern_quote", quote)
    monkeypatch.setattr(scan_server, "_capital_keepalive", keepalive, raising=False)
    monkeypatch.setattr(scan_server.aiohttp, "ClientSession", Client)
    monkeypatch.setattr(scan_server.asyncio, "sleep", stop_retry)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scan_server._capital_stream_loop())
    assert checks == [True]
    assert received == ([] if rejected else ["GOLD", "OIL_CRUDE"])
    assert (True in feeds) is not rejected
    assert feeds[-1] is False
    assert scan_server._STREAM_STATUS["connected"] is False
    assert scan_server._STREAM_STATUS["subscribed"] == []
    assert all(task.done() for task in tasks)


@pytest.mark.parametrize("status", [200, 401, 503])
def test_stream_session_check_renews_only_expired_sessions(monkeypatch, status):
    api = CapitalAPI()
    api.cst, api.x_security = "old", "old"
    logins = []

    async def login():
        logins.append(True)
        api.cst, api.x_security = "new", "new"
        return True

    monkeypatch.setattr(api, "login", login)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(status))
        ) as client:
            api._client = client
            await api.ensure_session()

    if status == 503:
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert len(logins) == (1 if status == 401 else 0)

"""Deterministic structure and entry contracts."""

import asyncio
from dataclasses import asdict

import pytest

from tools import scan_server
from tools.capital_api import CapitalAPI
from tools.structure_scan import Candle, analyze


def sample():
    ranges = [
        (9, 10, 9.5),
        (9, 11, 10),
        (10, 13, 12),
        (9, 11, 10),
        (9, 10, 9.5),
        (10, 15, 14),
        (14, 16, 15),
    ]
    return [
        Candle(i * 3600, (lo + hi) / 2, hi, lo, close)
        for i, (lo, hi, close) in enumerate(ranges)
    ]


def test_bos_and_pending_buy():
    result = analyze(sample())
    assert result["events"] == [
        {"index": 5, "direction": "buy", "kind": "BOS", "level": 13}
    ]
    assert result["setup"]["entry"] == 12
    assert result["setup"]["direction"] == "buy"


def test_sell_symmetry():
    bars = [
        Candle(b.time, 30 - b.open, 30 - b.low, 30 - b.high, 30 - b.close)
        for b in sample()
    ]
    result = analyze(bars)
    assert result["setup"]["direction"] == "sell"
    assert result["setup"]["entry"] == 18


def test_midpoint_touch_consumes_entry():
    bars = [*sample(), Candle(7 * 3600, 15, 16, 12, 14)]
    assert analyze(bars)["setup"] is None


def test_wick_does_not_break_structure():
    bars = sample()
    bars[5] = Candle(5 * 3600, 12, 15, 10, 12)
    bars[6] = Candle(6 * 3600, 12, 16, 10, 12)
    assert analyze(bars)["events"] == []
    assert analyze(bars)["setup"] is None


def test_choch_reversal():
    bars = [
        *sample(),
        Candle(7 * 3600, 14, 15, 9, 12),
        Candle(8 * 3600, 13, 14, 12, 13),
        Candle(9 * 3600, 13, 14, 12, 13),
        Candle(10 * 3600, 12, 13, 7, 8),
    ]
    assert analyze(bars)["events"][-1]["kind"] == "CHoCH"
    assert analyze(bars)["events"][-1]["direction"] == "sell"


@pytest.mark.parametrize(
    "bars",
    [
        [],
        sample()[:6],
        [*sample()[:-1], Candle(0, 1, 2, 0, 1)],
        [*sample()[:-1], Candle(25000, 1, 2, 0, float("nan"))],
    ],
)
def test_invalid_data(bars):
    with pytest.raises(ValueError):
        analyze(bars)


def test_api_excludes_open_candles(monkeypatch):
    class Broker:
        async def get_ohlcv(self, epic, timeframe, count):
            return [asdict(b) for b in sample()] + [
                asdict(Candle(25200, 15, 16, 14, 15))
            ]

    monkeypatch.setattr(scan_server, "API", Broker())
    monkeypatch.setattr(scan_server.time, "time", lambda: 26000)
    result = asyncio.run(scan_server.structure_scan("EURUSD", "HOUR"))
    assert len(result["bars"]) == 7
    assert result["setup"]["entry"] == 12


@pytest.mark.parametrize(
    ("symbol", "timeframe"), [("", "HOUR"), ("EUR USD", "HOUR"), ("EURUSD", "INVALID")]
)
def test_api_rejects_invalid_input(symbol, timeframe):
    with pytest.raises(scan_server.HTTPException) as error:
        asyncio.run(scan_server.structure_scan(symbol, timeframe))
    assert error.value.status_code == 422


def test_api_does_not_synthesize_missing_data(monkeypatch):
    class Broker:
        async def get_ohlcv(self, epic, timeframe, count):
            return []

    monkeypatch.setattr(scan_server, "API", Broker())
    with pytest.raises(scan_server.HTTPException) as error:
        asyncio.run(scan_server.structure_scan("EURUSD", "HOUR"))
    assert error.value.status_code == 422


def test_missing_session_rejects_cookie_less_requests(monkeypatch):
    monkeypatch.setattr(scan_server, "_WEB_SESSION_TOKEN", None)

    async def run():
        request = scan_server.Request(
            {"type": "http", "method": "GET", "path": "/api/account", "headers": []}
        )

        async def unexpected(request):
            raise AssertionError("Unauthenticated request reached handler")

        response = await scan_server.require_web_login(request, unexpected)
        assert response.status_code == 401

    asyncio.run(run())


def test_engine_pauses_without_broker_session(monkeypatch):
    monkeypatch.setattr(scan_server, "API", CapitalAPI(demo=True))
    monkeypatch.setattr(scan_server, "_get_mode", lambda: "demo")
    monkeypatch.setattr(scan_server, "_get_cursor", lambda: {"watchlist": ["EURUSD"]})
    monkeypatch.setattr(scan_server, "_save_cursor", lambda cursor: None)

    async def stop_on_sleep(seconds):
        raise asyncio.CancelledError

    calls = []

    async def unexpected(*args):
        calls.append(args)
        return ("EURUSD", [])

    monkeypatch.setattr(asyncio, "sleep", stop_on_sleep)
    monkeypatch.setattr(scan_server, "_fetch_live_symbol", unexpected)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(scan_server._live_engine_loop())
    assert not calls

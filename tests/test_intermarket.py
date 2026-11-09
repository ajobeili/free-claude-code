import asyncio

import httpx

from tools import intermarket
from tools.capital_api import BrokerSessionExpired, CapitalAPI


class FakeBroker(CapitalAPI):
    def __init__(
        self, closes: dict[str, list[float]], fail: frozenset[str] = frozenset()
    ):
        super().__init__(demo=True)
        self.closes = closes
        self.fail = fail
        self.calls: list[tuple[str, str, int]] = []

    async def get_ohlcv(
        self,
        epic: str,
        resolution: str = "HOUR",
        count: int = 500,
        from_time: float | None = None,
        to_time: float | None = None,
    ) -> list[dict]:
        self.calls.append((epic, resolution, count))
        if epic in self.fail:
            raise httpx.ConnectError("broker unreachable")
        return [{"close": c} for c in self.closes.get(epic, [])]

    async def search_markets(self, query: str) -> list[dict]:
        return [{"epic": query.upper()}]


def _fetch(broker, asset_class="all", now=1000.0):
    intermarket._CACHE.clear()
    return asyncio.run(intermarket.fetch_intermarket(broker, asset_class, now=now))


def test_reports_hourly_change_from_broker_bars():
    broker = FakeBroker({"DXY": [100.0, 101.0], "US500": [5000.0, 4950.0]})

    out = _fetch(broker)

    assert out == {
        "DXY": {"price": 101.0, "change_1h_pct": 1.0},
        "US500": {"price": 4950.0, "change_1h_pct": -1.0},
    }
    assert ("DXY", "HOUR", 2) in broker.calls


def test_unpriced_or_failing_symbols_are_omitted_not_invented():
    broker = FakeBroker({"US100": [1.0], "VIX": [20.0, 21.0]}, fail=frozenset({"DXY"}))

    out = _fetch(broker)

    assert set(out) == {"VIX"}


def test_expired_broker_session_yields_empty_context():
    class Expired(FakeBroker):
        async def get_ohlcv(
            self,
            epic: str,
            resolution: str = "HOUR",
            count: int = 500,
            from_time: float | None = None,
            to_time: float | None = None,
        ) -> list[dict]:
            raise BrokerSessionExpired("log in again")

    assert _fetch(Expired({})) == {}


def test_slow_symbol_times_out_without_blocking_the_rest(monkeypatch):
    class Slow(FakeBroker):
        async def get_ohlcv(
            self,
            epic: str,
            resolution: str = "HOUR",
            count: int = 500,
            from_time: float | None = None,
            to_time: float | None = None,
        ) -> list[dict]:
            if epic == "VIX":
                await asyncio.sleep(10)
            return await super().get_ohlcv(epic, resolution, count)

    monkeypatch.setattr(intermarket, "_FETCH_TIMEOUT_S", 0.05)
    out = _fetch(Slow({"DXY": [1.0, 2.0], "VIX": [1.0, 2.0]}))

    assert set(out) == {"DXY"}


def test_asset_class_filter_and_cache():
    broker = FakeBroker({"DXY": [1.0, 1.0], "US500": [1.0, 2.0]})

    first = _fetch(broker, "fx")
    calls = len(broker.calls)
    cached = asyncio.run(intermarket.fetch_intermarket(broker, "fx", now=1010.0))

    assert set(first) == {"DXY"}
    assert cached == first
    assert len(broker.calls) == calls

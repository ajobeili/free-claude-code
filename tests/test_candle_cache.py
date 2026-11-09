import asyncio

from tools import scan_server


def test_cached_ohlcv_refreshes_only_current_and_new_bars(monkeypatch):
    class FakeAPI:
        def __init__(self):
            self.calls = []

        async def get_ohlcv(
            self, epic, resolution, count, from_time=None, to_time=None
        ):
            self.calls.append((epic, resolution, count, from_time, to_time))
            if from_time is None:
                return [
                    {"time": 100, "open": 1, "high": 2, "low": 0, "close": 1},
                    {"time": 200, "open": 1, "high": 3, "low": 1, "close": 2},
                ]
            return [
                {"time": 200, "open": 1, "high": 4, "low": 1, "close": 3},
                {"time": 300, "open": 3, "high": 5, "low": 2, "close": 4},
            ]

    api = FakeAPI()
    monkeypatch.setattr(scan_server, "API", api)
    monkeypatch.setattr(scan_server, "_CANDLE_CACHE", {})

    async def run():
        first = await scan_server._cached_ohlcv("GOLD", "MINUTE_15", 3, now=1000)
        second = await scan_server._cached_ohlcv("GOLD", "MINUTE_15", 3, now=1001)
        return first, second

    first, second = asyncio.run(run())
    assert [bar["time"] for bar in first] == [100, 200]
    assert [bar["time"] for bar in second] == [100, 200, 300]
    assert second[1]["close"] == 3
    assert api.calls[0][2:] == (3, None, None)
    assert api.calls[1][2] == 3
    assert api.calls[1][3] == 200
    assert api.calls[1][4] == 941

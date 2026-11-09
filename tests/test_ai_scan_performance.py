"""AI scan must overlap independent symbols without unbounded LLM requests."""

import asyncio

from tests.test_structure_scan import scan_server
from tools import agent_orchestrator, intermarket, news, strategy_engine


def test_ai_scan_concurrency_and_shared_context(monkeypatch):
    active = 0
    peak = 0
    calls = []
    market_calls = []

    class Broker:
        async def get_ohlcv(self, *args):
            return [
                {
                    "time": i * 3600,
                    "open": 100 + i * 0.1,
                    "high": 102 + i * 0.1,
                    "low": 99 + i * 0.1,
                    "close": 101 + i * 0.1,
                    "volume": 100,
                }
                for i in range(300)
            ]

    def signal(bars, symbol):
        return strategy_engine.Signal(
            "test", symbol, "long", 100, 99, 102, 0.7, "test", 1
        )

    async def evaluate(sig, *args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        calls.append(sig["symbol"])
        try:
            await asyncio.sleep(0.05)
            return {"verdict": "approve", "score": 75, "consensus": "approve"}
        finally:
            active -= 1

    async def context(*args):
        market_calls.append(1)
        return {}

    monkeypatch.setattr(scan_server, "API", Broker())
    monkeypatch.setattr(scan_server, "ALL_STRATEGIES", {"test": signal})
    monkeypatch.setattr(strategy_engine, "backtest", lambda *args, **kwargs: {})
    monkeypatch.setattr(agent_orchestrator, "evaluate_signal_v2", evaluate)
    monkeypatch.setattr(news, "fetch_news", lambda *args, **kwargs: [])
    monkeypatch.setattr(intermarket, "fetch_intermarket", context)
    result = asyncio.run(
        scan_server.scan(
            scan_server.ScanRequest(
                symbols=["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"],
                strategy_filter=["test"],
                use_ai=True,
            )
        )
    )
    assert len(result["setups"]) == 4
    assert sorted(calls) == ["AUDUSD", "EURUSD", "GBPUSD", "USDJPY"]
    assert 1 < peak <= 3
    assert market_calls == [1]

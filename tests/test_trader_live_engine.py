"""Responsiveness contracts for the FCC Trader live engine."""

import asyncio
import json
import time

import httpx
import pytest

from tools import agent_orchestrator, scan_server
from tools.capital_api import CapitalAPI
from tools.config import get_settings
from tools.strategy_engine import Signal


class FakeAccount:
    balance = 50.0
    min_deal_size = 0.0
    currency = "USD"
    demo = True


class FakeMarketInfo:
    margin_factor = 20.0
    min_deal_size = 0.01
    max_deal_size = 10.0
    step = 0.01
    point = 0.0001
    contract_size = 1.0
    currency = "USD"
    bid = 1.1
    offer = 1.1002


class FakeDemoAPI:
    demo = True
    cst = "cst"
    x_security = "xsec"

    def __init__(self) -> None:
        self.orders: list[dict] = []

    async def get_positions(self) -> list:
        return []

    async def get_account(self) -> FakeAccount:
        return FakeAccount()

    async def get_market_info(self, epic: str) -> FakeMarketInfo:
        return FakeMarketInfo()

    async def search_markets(self, query: str) -> list:
        return []

    async def place_order(
        self, epic, direction, size, sl=None, tp=None, account_id=None
    ):
        order = {
            "epic": epic,
            "direction": direction,
            "size": size,
            "sl": sl,
            "tp": tp,
            "account_id": account_id,
        }
        self.orders.append(order)
        return {"deal_id": "D1", "status": "OPEN", **order}


def test_live_symbol_evaluation_does_not_block_event_loop(monkeypatch) -> None:
    """CPU scoring must run in a worker so request handlers can still run."""

    def slow_evaluation(
        symbol: str, bars: list[dict]
    ) -> tuple[list[dict], dict[str, int]]:
        time.sleep(0.05)
        return [], {symbol: len(bars)}

    monkeypatch.setattr(scan_server, "_evaluate_live_symbol", slow_evaluation)

    async def run() -> list[tuple[list[dict], dict[str, int]]]:
        task = asyncio.create_task(
            scan_server._evaluate_live_symbols(
                [("EURUSD", [{"close": 1.0}]), ("GBPUSD", [{"close": 1.0}])]
            )
        )
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.01)
        return await task

    assert asyncio.run(run()) == [([], {"EURUSD": 1}), ([], {"GBPUSD": 1})]


def test_live_signal_values_match_strategy_signal_fields() -> None:
    signal = Signal("test", "EURUSD", "short", 1.1, 1.2, 1.0, 0.5, "test", 0.01)

    assert scan_server._signal_direction(signal) == "SHORT"
    assert scan_server._signal_value(signal, "entry") == 1.1


def test_capital_client_bypasses_inherited_proxy(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        async def aclose(self) -> None:
            pass

    def build_client(**kwargs: object) -> FakeClient:
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr("tools.capital_api.httpx.AsyncClient", build_client)

    async def create_and_close() -> None:
        client = CapitalAPI()
        async with client:
            assert client._client is not None

    asyncio.run(create_and_close())

    assert captured["trust_env"] is False


def test_default_fcc_proxy_uses_the_running_local_server(monkeypatch) -> None:
    monkeypatch.delenv("FCC_PROXY_URL", raising=False)

    assert get_settings().fcc_proxy_url == "http://localhost:8082/v1/messages"


def test_agent_requests_use_the_fcc_bearer_token(monkeypatch) -> None:
    monkeypatch.setenv("FCC_PROXY_AUTH_TOKEN", "local-token")
    config = agent_orchestrator.AgentConfig("test", "openrouter", "model:free", "")

    headers = agent_orchestrator._proxy_headers(config)

    assert headers["Authorization"] == "Bearer local-token"
    assert "x-api-key" not in headers


def test_default_agents_use_the_configured_free_provider(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("LLM_META_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_META_MODEL", raising=False)
    monkeypatch.setattr(agent_orchestrator, "PLANNER_AI_FILE", tmp_path / "none.json")

    assert {agent.provider_type for agent in agent_orchestrator.AGENTS} == {
        "openrouter"
    }
    assert agent_orchestrator.RISK_MANAGER.provider_type == "openrouter"
    assert agent_orchestrator._resolve_meta_config().model.endswith(":free")


def test_meta_config_prefers_saved_planner_choice(monkeypatch, tmp_path) -> None:
    saved = tmp_path / "planner-ai.json"
    saved.write_text('{"provider": "openrouter", "model": "openrouter/free"}')
    monkeypatch.setattr(agent_orchestrator, "PLANNER_AI_FILE", saved)

    assert agent_orchestrator._resolve_meta_config().model == "openrouter/free"


def test_meta_config_ignores_malformed_saved_choice(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LLM_META_MODEL", "env/model:free")
    saved = tmp_path / "planner-ai.json"
    saved.write_text("{not json")
    monkeypatch.setattr(agent_orchestrator, "PLANNER_AI_FILE", saved)

    assert agent_orchestrator._resolve_meta_config().model == "env/model:free"


def test_save_planner_model_writes_the_file_the_resolver_reads(
    monkeypatch, tmp_path
) -> None:
    saved = tmp_path / "planner-ai.json"
    monkeypatch.setattr(agent_orchestrator, "PLANNER_AI_FILE", saved)

    asyncio.run(
        scan_server.save_planner_ai_model(
            scan_server.PlannerAIModel(provider="OpenRouter", model="x/y:free")
        )
    )

    assert agent_orchestrator._resolve_meta_config().model == "x/y:free"


def test_proxy_model_ref_keeps_router_model_and_other_providers() -> None:
    ref = agent_orchestrator._proxy_model_ref
    assert ref("openrouter", "openrouter/free") == "open_router/openrouter/free"
    assert ref("groq", "llama3") == "groq/llama3"
    assert ref("groq", "groq/llama3") == "groq/llama3"


def test_agent_routes_with_explicit_provider_model(monkeypatch):
    captured = []
    original_client = httpx.AsyncClient

    def handle(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    def client(**kwargs):
        return original_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(agent_orchestrator.httpx, "AsyncClient", client)

    async def run():
        for model in (
            "google/example:free",
            "openrouter/google/example:free",
            "open_router/google/example:free",
        ):
            config = agent_orchestrator.AgentConfig("test", "openrouter", model, "")
            result = await agent_orchestrator.call_agent(config, "test")
            assert result["text"] == "ok"

    asyncio.run(run())
    assert [body["model"] for body in captured] == [
        "open_router/google/example:free",
        "open_router/google/example:free",
        "open_router/google/example:free",
    ]


def test_demo_auto_trade_refuses_live_api(monkeypatch) -> None:
    api = FakeDemoAPI()
    api.demo = False
    monkeypatch.setattr(scan_server, "API", api)
    monkeypatch.setattr(scan_server, "_get_mode", lambda: "demo")

    result = asyncio.run(
        scan_server._maybe_execute_demo_signal(
            {
                "symbol": "EURUSD",
                "direction": "LONG",
                "entry": 1.1,
                "stop_loss": 1.09,
                "take_profit": 1.12,
            },
            {"enabled": True, "risk_pct": 0.005, "max_open_positions": 1},
        )
    )

    assert result == {
        "status": "skipped",
        "reason": "Capital.com session is not demo",
    }
    assert api.orders == []


def test_demo_auto_trade_submits_demo_order(monkeypatch) -> None:
    api = FakeDemoAPI()
    monkeypatch.setattr(scan_server, "API", api)
    monkeypatch.setattr(scan_server, "_get_mode", lambda: "demo")

    result = asyncio.run(
        scan_server._maybe_execute_demo_signal(
            {
                "symbol": "EURUSD",
                "direction": "SHORT",
                "entry": 1.1,
                "stop_loss": 1.11,
                "take_profit": 1.08,
                "size_modifier": 0.5,
            },
            {"enabled": True, "risk_pct": 0.005, "max_open_positions": 1},
        )
    )

    assert result is not None
    assert result["status"] == "submitted"
    assert api.orders == [
        {
            "epic": "EURUSD",
            "direction": "SELL",
            "size": 10.0,
            "sl": 1.11,
            "tp": 1.08,
            "account_id": None,
        }
    ]


class FakeBarsAPI:
    demo = True
    cst = "cst"
    x_security = "xsec"

    def __init__(self, bars: list[dict]) -> None:
        self.bars = bars
        self.requests: list[tuple[str, str, int]] = []

    async def search_markets(self, query: str) -> list:
        return []

    async def get_ohlcv(self, epic: str, resolution: str, count: int) -> list[dict]:
        self.requests.append((epic, resolution, count))
        return self.bars


def _trend_bars(n: int = 260) -> list[dict]:
    return [
        {
            "time": i * 3600,
            "open": 100 + i * 0.1,
            "high": 100.5 + i * 0.1,
            "low": 99.5 + i * 0.1,
            "close": 100 + i * 0.1 + (0.2 if i % 2 else -0.2),
            "volume": 1000 + i,
        }
        for i in range(n)
    ]


def test_live_bars_use_resolved_epic_and_requested_resolution(monkeypatch) -> None:
    api = FakeBarsAPI(_trend_bars())
    monkeypatch.setattr(scan_server, "API", api)

    bars = asyncio.run(scan_server._fetch_bars_for("XAUUSD", 1000, "MINUTE_15"))

    assert api.requests == [("GOLD", "MINUTE_15", 1000)]
    assert bars == api.bars


def test_live_bars_never_fall_back_to_synthetic_data(monkeypatch) -> None:
    monkeypatch.setattr(scan_server, "API", FakeBarsAPI([]))
    assert asyncio.run(scan_server._fetch_bars_for("EURUSD", 1000)) == []

    logged_out = FakeBarsAPI(_trend_bars())
    logged_out.cst = ""
    monkeypatch.setattr(scan_server, "API", logged_out)
    assert asyncio.run(scan_server._fetch_bars_for("EURUSD", 1000)) == []
    assert logged_out.requests == []


def test_live_symbol_skipped_when_broker_has_too_few_bars(monkeypatch) -> None:
    monkeypatch.setattr(scan_server, "API", FakeBarsAPI(_trend_bars(100)))

    result = asyncio.run(
        scan_server._fetch_live_symbol("EURUSD", "HOUR", asyncio.Semaphore(1))
    )

    assert result == ("EURUSD", [])


def test_indicator_snapshot_reports_real_latest_bar_values() -> None:
    bars = _trend_bars()
    snap = scan_server._indicator_snapshot(bars)

    assert snap["i"] == len(bars) - 1
    assert snap["last_close"] == bars[-1]["close"]
    assert snap["ema_trend"] == "up"
    assert snap["atr"] > 0
    assert snap["adx"] > 0
    assert 0 <= snap["bb_pct"] <= 100
    assert snap["rsi"] != 50.0


def test_live_results_carry_indicator_context(monkeypatch) -> None:
    from tools import strategy_engine

    bars = _trend_bars()
    signal = Signal("trend", "EURUSD", "long", 126.0, 125.0, 128.0, 0.6, "t", 0.5)
    monkeypatch.setattr(
        strategy_engine, "ALL_STRATEGIES", {"trend": lambda bars, sym: signal}
    )
    monkeypatch.setattr(
        strategy_engine,
        "compute_setup_score",
        lambda backtest, regime, name: {"score": 80.0},
    )

    results, hits = scan_server._evaluate_live_symbol("EURUSD", bars)

    assert hits == {"trend": 1}
    assert results[0]["context"] == scan_server._indicator_snapshot(bars)


def test_ohlcv_endpoint_reports_missing_broker_bars(monkeypatch) -> None:
    monkeypatch.setattr(scan_server, "API", FakeBarsAPI([]))

    with pytest.raises(scan_server.HTTPException) as raised:
        asyncio.run(scan_server.ohlcv("EURUSD"))

    assert raised.value.status_code == 502


def test_lifespan_runs_startup_and_shutdown_hooks(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    calls: list[str] = []

    async def init() -> None:
        calls.append("init")

    async def shutdown() -> None:
        calls.append("shutdown")

    monkeypatch.setattr(scan_server, "_init", init)
    monkeypatch.setattr(scan_server, "_shutdown", shutdown)

    with TestClient(scan_server.app):
        assert calls == ["init"]

    assert calls == ["init", "shutdown"]

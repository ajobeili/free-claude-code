import asyncio

from tools import agent_orchestrator


def test_openrouter_route_preserves_nested_model_name(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        def json(self):
            return {"content": [{"type": "text", "text": "OK"}], "usage": {}}

        @property
        def text(self):
            return ""

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return None

        async def post(self, url, *, headers, json):
            captured["body"] = json
            return Response()

    monkeypatch.setattr(
        agent_orchestrator.httpx, "AsyncClient", lambda **kwargs: Client()
    )

    result = asyncio.run(
        agent_orchestrator.call_agent(
            agent_orchestrator.AgentConfig(
                name="test",
                provider_type="openrouter",
                model="openrouter/free",
                system="",
            ),
            "Reply with OK.",
        )
    )

    assert result["text"] == "OK"
    assert captured["body"]["model"] == "open_router/openrouter/free"
    assert agent_orchestrator._is_free_tier("openrouter", "openrouter/free")


def test_proxy_header_uses_configured_token(monkeypatch):
    monkeypatch.setenv("FCC_PROXY_AUTH_TOKEN", "proxy-secret")

    headers = agent_orchestrator._proxy_headers(
        agent_orchestrator.AgentConfig(
            name="test",
            provider_type="openrouter",
            model="openrouter/free",
            system="",
        )
    )

    assert headers["Authorization"] == "Bearer proxy-secret"


def test_agent_exception_is_recorded_as_an_errored_vote(monkeypatch):
    first = agent_orchestrator.AGENTS[0].name

    async def answer(config, prompt, max_tokens=512):
        if config.name == first:
            raise RuntimeError("agent crashed")
        return {"text": '{"verdict": "approve", "confidence": 0.8}', "latency_ms": 5}

    monkeypatch.setattr(agent_orchestrator, "call_agent", answer)

    result = asyncio.run(
        agent_orchestrator.evaluate_signal(
            {"symbol": "EURUSD", "direction": "long"}, {}
        )
    )

    crashed = next(v for v in result["votes"] if v["agent"] == first)
    assert crashed == {"agent": first, "error": "agent crashed"}
    assert result["agent_latencies"][first] == 0
    assert result["approve_count"] == len(agent_orchestrator.AGENTS) - 1

import asyncio

import pytest

from tests.test_structure_scan import scan_server


@pytest.mark.parametrize(
    "error,status",
    [
        ("HTTP 401", "authentication_error"),
        ("HTTP 429", "rate_limited"),
        ("HTTP 402", "credits_required"),
        ("timeout", "timeout"),
        ("blocked", "unavailable"),
    ],
)
def test_ai_status_errors(monkeypatch, error, status):
    async def answer(*args):
        return {"error": error}

    monkeypatch.setattr(scan_server, "choose_trade_plan", answer)
    result = asyncio.run(scan_server.planner_ai_check())
    assert result["status"] == status


def test_ai_status_success(monkeypatch):
    async def answer(*args):
        return {
            "text": '{"candidate_id":null,"reasoning":"Connection check","counterargument":"No market data"}'
        }

    monkeypatch.setattr(scan_server, "choose_trade_plan", answer)
    assert asyncio.run(scan_server.planner_ai_check())["status"] == "available"


@pytest.mark.parametrize("applied", [True, False])
def test_save_key_uses_proxy_without_returning_secret(monkeypatch, applied):
    import httpx
    from pydantic import SecretStr

    original = httpx.AsyncClient
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json={"applied": applied, "pending_fields": []})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
    )

    async def run():
        request = scan_server.PlannerAIKey(api_key=SecretStr("test-secret"))
        if applied:
            result = await scan_server.save_planner_ai_key(request)
            assert result["saved"] is True
            assert "test-secret" not in str(result)
        else:
            with pytest.raises(scan_server.HTTPException):
                await scan_server.save_planner_ai_key(request)

    asyncio.run(run())
    assert captured[0].url.path == "/admin/api/config/apply"

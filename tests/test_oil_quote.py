import asyncio

import httpx
import pytest

from tools import scan_server
from tools.capital_api import CapitalAPI
from tools.trade_planner import quote_from


def test_oil_quote_uses_search_name_and_exact_epic(monkeypatch):
    def handler(request):
        assert request.url.params["searchTerm"] == "Crude"
        return httpx.Response(
            200,
            json={
                "markets": [
                    {"epic": "SCO", "bid": 1, "offer": 2},
                    {
                        "epic": "OIL_CRUDE",
                        "bid": 96,
                        "offer": 96.04,
                        "marketStatus": "TRADEABLE",
                    },
                ]
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            broker = CapitalAPI(demo=True)
            broker._client = client
            monkeypatch.setattr(scan_server, "API", broker)
            monkeypatch.setattr(scan_server, "_TICK_CACHE", {})
            result = await scan_server.tick("OIL_CRUDE")
            assert result["bid"] == 96
            assert result["market_status"] == "TRADEABLE"

    asyncio.run(run())


def test_missing_quote_is_not_reported_closed():
    with pytest.raises(ValueError, match="lookup failed"):
        quote_from({"error": "not found"}, 0)

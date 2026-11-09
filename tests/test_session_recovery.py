"""Session recovery contracts, without external broker access."""

import asyncio

import httpx
import pytest

from tests.test_structure_scan import scan_server
from tools.capital_api import BrokerSessionExpired, CapitalAPI


def test_concurrent_expired_candles_share_one_login(monkeypatch):
    async def run():
        api = CapitalAPI()
        api.cst, api.x_security = "old", "old"
        logins = []

        async def login():
            logins.append(1)
            await asyncio.sleep(0)
            api.cst, api.x_security = "new", "new"
            return True

        def respond(request):
            return httpx.Response(
                401 if request.headers.get("CST") != "new" else 200, json={"prices": []}
            )

        monkeypatch.setattr(api, "login", login)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            api._client = client
            assert (
                await asyncio.gather(*(api.get_ohlcv("GOLD") for _ in range(7)))
                == [[]] * 7
            )
        assert len(logins) == 1

    asyncio.run(run())


def test_failed_recovery_does_not_storm_login(monkeypatch):
    async def run():
        api = CapitalAPI()
        api.cst, api.x_security = "old", "old"
        logins = []

        async def login():
            logins.append(1)
            return False

        monkeypatch.setattr(api, "login", login)
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(401))
        ) as client:
            api._client = client
            for _ in range(3):
                with pytest.raises(BrokerSessionExpired):
                    await api.get_ohlcv("GOLD")
        assert len(logins) == 1
        assert api.cst is None

    asyncio.run(run())


def test_structure_returns_auth_error_for_expired_broker(monkeypatch):
    class Broker:
        async def get_ohlcv(self, *args):
            raise BrokerSessionExpired("Please log in")

    monkeypatch.setattr(scan_server, "API", Broker())
    with pytest.raises(scan_server.HTTPException) as error:
        asyncio.run(scan_server.structure_scan("EURUSD"))
    assert error.value.status_code == 401

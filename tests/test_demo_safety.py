import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import Response

from tools import scan_server, state
from tools.capital_api import Account, CapitalAPI, SymbolInfo, compute_size_capital


def _account() -> Account:
    return Account(
        account_id="DEMO-1",
        account_name="Demo",
        account_type="CFD",
        balance=10_000.0,
        deposit=10_000.0,
        profit_loss=0.0,
        available=10_000.0,
        currency="USD",
        leverage=20,
        demo=True,
    )


def _market_info(*, min_size: float = 0.1, min_stop: float = 0.0) -> SymbolInfo:
    return SymbolInfo(
        epic="EURUSD",
        name="EUR/USD",
        instrument_type="CURRENCIES",
        digits=5,
        point=0.0001,
        min_deal_size=min_size,
        max_deal_size=100.0,
        step=0.01,
        bid=1.1,
        offer=1.1002,
        spread=0.0002,
        currency="USD",
        margin_factor=20.0,
        min_stop_distance=min_stop,
        contract_size=100_000.0,
    )


def test_capital_client_refuses_live_login_before_network_access() -> None:
    api = CapitalAPI(demo=False)
    api.api_key = "key"
    api.login_id = "email@example.com"
    api.password = "password"

    assert asyncio.run(api.login()) is False


def test_capital_client_refuses_live_order_before_network_access() -> None:
    api = CapitalAPI(demo=False)

    result = asyncio.run(api.place_order("EURUSD", "BUY", 1.0))

    assert result == {"error": "live trading is disabled; use a demo account"}


def test_login_endpoint_rejects_live_environment(monkeypatch) -> None:
    called = False

    async def unexpected_login() -> bool:
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(scan_server.API, "login", unexpected_login)
    request = scan_server.LoginRequest(
        capital_api_key="key",
        capital_login="email@example.com",
        capital_password="password",
        capital_environment="live",
    )

    with pytest.raises(scan_server.HTTPException) as raised:
        asyncio.run(scan_server.login(request, Response()))

    assert raised.value.status_code == 403
    assert called is False


def test_execute_endpoint_rejects_non_demo_session(monkeypatch) -> None:
    api = CapitalAPI(demo=False)
    api.cst = "cst"
    monkeypatch.setattr(scan_server, "API", api)

    with pytest.raises(scan_server.HTTPException) as raised:
        asyncio.run(
            scan_server.execute(
                scan_server.ExecuteRequest(epic="EURUSD", direction="BUY", size=1.0)
            )
        )

    assert raised.value.status_code == 403


def test_execute_endpoint_calculates_size_from_demo_risk_not_client_size(
    monkeypatch,
) -> None:
    class DemoAPI:
        demo = True
        cst = "cst"

        def __init__(self) -> None:
            self.orders: list[float] = []

        async def get_account(self) -> Account:
            return _account()

        async def get_market_info(self, epic: str) -> SymbolInfo:
            assert epic == "EURUSD"
            return _market_info()

        async def place_order(
            self,
            epic: str,
            direction: str,
            size: float,
            sl: float | None = None,
            tp: float | None = None,
            account_id: str | None = None,
        ) -> dict[str, object]:
            self.orders.append(size)
            return {"deal_id": "D1", "size": size}

    api = DemoAPI()
    monkeypatch.setattr(scan_server, "API", api)

    result = asyncio.run(
        scan_server.execute(
            scan_server.ExecuteRequest(
                epic="EURUSD",
                direction="BUY",
                size=99.0,
                sl=1.0992,
                tp=1.102,
                risk_pct=0.005,
            )
        )
    )

    assert api.orders == [0.5]
    assert result["size"] == 0.5


def test_cockpit_rejects_live_mode() -> None:
    result = asyncio.run(
        scan_server.set_cockpit_mode(scan_server.ModeRequest(mode="live"))
    )

    assert result == {"error": "invalid mode: live"}
    with pytest.raises(ValueError):
        state.set_mode("live")


def test_missing_account_is_reported_instead_of_fabricated(monkeypatch) -> None:
    api = CapitalAPI()
    monkeypatch.setattr(scan_server, "API", api)

    assert asyncio.run(api.get_account()) is None
    with pytest.raises(scan_server.HTTPException) as raised:
        asyncio.run(scan_server.account())

    assert raised.value.status_code == 503


def test_position_size_uses_contract_value_and_rounds_down() -> None:
    size = compute_size_capital(
        balance=10_000.0,
        risk_pct=0.01,
        entry=1.1,
        sl_price=1.099,
        contract_size=100_000.0,
        min_size=0.01,
        max_size=100.0,
        step=0.01,
    )

    assert size == 1.0


def test_position_size_refuses_broker_minimum_that_exceeds_risk_budget() -> None:
    with pytest.raises(ValueError, match="minimum deal size"):
        compute_size_capital(
            balance=100.0,
            risk_pct=0.005,
            entry=100.0,
            sl_price=99.0,
            contract_size=1.0,
            min_size=1.0,
            max_size=100.0,
            step=1.0,
        )


def test_place_order_refuses_to_move_stop_or_increase_size(monkeypatch) -> None:
    api = CapitalAPI(demo=True)
    api.accounts = [_account()]
    api._client = httpx.AsyncClient()

    async def market_info(epic: str) -> SymbolInfo:
        assert epic == "EURUSD"
        return _market_info(min_size=1.0, min_stop=0.01)

    monkeypatch.setattr(api, "get_market_info", market_info)

    too_small = asyncio.run(api.place_order("EURUSD", "BUY", 0.5))
    too_close = asyncio.run(api.place_order("EURUSD", "BUY", 1.0, sl=1.099))
    asyncio.run(api._client.aclose())

    assert too_small["error"].startswith("requested size 0.5 is below broker minimum")
    assert too_close["error"].startswith("stop distance")


def test_workbench_has_no_live_trading_controls_or_sample_profit() -> None:
    html = (
        Path(scan_server.__file__)
        .with_name("fcc_trader.html")
        .read_text(encoding="utf-8")
    )

    assert 'data-mode="live"' not in html
    assert '<option value="live">' not in html
    assert "+$124.50" not in html
    assert "10124.50" not in html
    assert "$50/trade on $10K planning balance" not in html
    assert "updateRisk(account.riskPct * 100);" in html


def test_scan_never_generates_setups_from_missing_broker_bars(monkeypatch) -> None:
    async def no_bars(epic: str, resolution: str, count: int) -> list[dict]:
        return []

    async def same_epic(api: object, symbol: str) -> str:
        return symbol

    monkeypatch.setattr(scan_server, "_cached_ohlcv", no_bars)
    monkeypatch.setattr(scan_server, "resolve_epic", same_epic)

    result = asyncio.run(scan_server.scan(scan_server.ScanRequest(symbols=["EURUSD"])))

    assert result["setups"] == []
    assert result["found"] == 0
    assert result["errors"] == [
        {
            "symbol": "EURUSD",
            "epic": "EURUSD",
            "issue": "no usable broker bars (received 0; need 220)",
        }
    ]

from fastapi.testclient import TestClient

from tools import scan_server


def test_app_routes(monkeypatch):
    async def noop() -> None:
        return None

    monkeypatch.setattr(scan_server, "_init", noop)
    monkeypatch.setattr(scan_server, "_shutdown", noop)
    pages = {
        "/": "trader_home.html",
        "/workbench": "fcc_trader.html",
        "/structure": "structure_scan.html",
        "/planner": "trade_planner.html",
        "/pattern": "pattern_scan.html",
    }
    with TestClient(scan_server.app) as client:
        for route, filename in pages.items():
            response = client.get(route)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            source = (scan_server._HTML_PATH.parent / filename).read_bytes()
            assert response.content == source

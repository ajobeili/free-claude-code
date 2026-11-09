import pytest
from fastapi.testclient import TestClient

from tools import scan_server

LOCAL = ("127.0.0.1", 50000)
REMOTE = ("192.168.1.50", 50000)


@pytest.fixture
def restored_session(monkeypatch):
    async def noop() -> None:
        return None

    monkeypatch.setattr(scan_server, "_init", noop)
    monkeypatch.setattr(scan_server, "_shutdown", noop)
    monkeypatch.setattr(scan_server, "_WEB_SESSION_TOKEN", "restored-token")


def test_remote_client_is_not_handed_the_restored_session(restored_session):
    with TestClient(scan_server.app, client=REMOTE) as client:
        page = client.get("/")
        api = client.get("/api/session")

    assert "fcc_session" not in page.cookies
    assert api.status_code == 401
    assert "fcc_session" not in api.cookies


def test_remote_client_cannot_reuse_a_refreshed_cookie_it_never_had(restored_session):
    with TestClient(scan_server.app, client=REMOTE) as client:
        client.get("/api/session")
        second = client.get("/api/session")

    assert second.status_code == 401


def test_loopback_browser_recovers_session_after_restart(restored_session):
    with TestClient(scan_server.app, client=LOCAL) as client:
        client.cookies.set("fcc_session", "stale-token-from-previous-run")
        first = client.get("/api/session")
        assert first.status_code == 401
        assert first.cookies.get("fcc_session") == "restored-token"
        client.cookies.set("fcc_session", "restored-token")
        assert client.get("/api/session").status_code == 200


def test_loopback_page_load_grants_restored_session(restored_session):
    with TestClient(scan_server.app, client=LOCAL) as client:
        page = client.get("/")

    assert page.cookies.get("fcc_session") == "restored-token"


def test_api_does_not_allow_cross_origin_credentialed_reads(restored_session):
    with TestClient(scan_server.app, client=LOCAL) as client:
        client.cookies.set("fcc_session", "restored-token")
        response = client.get(
            "/api/session", headers={"Origin": "https://evil.example"}
        )

    assert "access-control-allow-origin" not in response.headers


def test_reauth_keeps_the_caller_signed_in_after_rotating_the_token(
    restored_session, monkeypatch
):
    async def fake_login() -> bool:
        return True

    monkeypatch.setattr(scan_server.API, "login", fake_login)
    with TestClient(scan_server.app, client=REMOTE) as client:
        client.cookies.set("fcc_session", "restored-token")
        reauth = client.post("/api/reauth")
        assert reauth.json()["ok"] is True
        rotated = reauth.cookies.get("fcc_session")
        assert rotated and rotated != "restored-token"
        client.cookies.set("fcc_session", rotated)
        assert client.get("/api/session").status_code == 200

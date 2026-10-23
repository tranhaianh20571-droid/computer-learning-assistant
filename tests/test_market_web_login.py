"""Web login handoff uses isolated identities, databases and one-time tickets."""
# ruff: noqa: F811 -- imported pytest fixtures are injected by parameter name
from datetime import timedelta

import pytest
from backend.auth.models import UserRow
from backend.auth.service import AuthService
from backend.market_link import BindingStore, MarketClient, MarketError
from backend.market_router import create_market_router
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from test_market_bridge import env  # noqa: F401
from test_market_registration import market  # noqa: F401
from test_market_uploads import signed_client


def issue(market, owner="synthetic-alice"):
    web = signed_client(market, owner)
    token = web.post("/api/devices").json()["token"]
    headers = {"Authorization": "Bearer " + token}
    result = web.post("/api/web-login/tickets", headers=headers)
    assert result.status_code == 200
    assert result.headers["cache-control"] == "private, no-store"
    return web, token, result.json()["ticket"]


def test_ticket_switches_browser_to_device_owner_and_is_single_use(market):
    web, token, ticket = issue(market)
    browser = signed_client(market, "synthetic-bob")
    origin = {"Origin": market.BASE_URL}
    assert browser.post("/api/web-login/consume", json={"ticket": ticket}).status_code == 403
    result = browser.post("/api/web-login/consume", json={"ticket": ticket}, headers=origin)
    assert result.status_code == 200
    assert "HttpOnly" in result.headers["set-cookie"]
    assert "Secure" in result.headers["set-cookie"]
    assert result.headers["cache-control"] == "private, no-store"
    assert browser.get("/api/me").json()["user"]["user_id"] == "synthetic-alice"
    assert browser.post("/api/web-login/consume", json={"ticket": ticket}, headers=origin).status_code == 410
    assert token not in str(result.json())
    with market.db() as conn:
        assert conn.execute("SELECT count(*) FROM web_login_tickets").fetchone()[0] == 0
    web.close()
    browser.close()


@pytest.mark.parametrize("invalid", ["expired", "revoked", "receive", "unverified"])
def test_invalid_ticket_cannot_open_session(market, invalid):
    web, _, ticket = issue(market)
    with market.db() as conn:
        if invalid == "expired":
            conn.execute("UPDATE web_login_tickets SET expires_at=?", (market.iso(market.now() - timedelta(seconds=1)),))
        elif invalid == "revoked":
            conn.execute("DELETE FROM devices")
        elif invalid == "receive":
            conn.execute("UPDATE devices SET purpose='receive'")
        else:
            conn.execute("UPDATE users SET verified=0")
    with TestClient(market.app, base_url="https://market.invalid") as browser:
        result = browser.post("/api/web-login/consume", json={"ticket": ticket}, headers={"Origin": market.BASE_URL})
        assert result.status_code == 410
        assert "set-cookie" not in result.headers
    web.close()


def test_receive_credentials_can_read_name_but_cannot_issue_web_login(market):
    web, token, _ = issue(market)
    with market.db() as conn:
        conn.execute("UPDATE devices SET purpose='receive'")
    headers = {"Authorization": "Bearer " + token}
    assert web.get("/api/local/profile", headers=headers).json()["display_name"] == "synthetic-alice"
    assert web.post("/api/web-login/tickets", headers=headers).status_code == 403
    web.close()


def test_bridge_syncs_existing_name_without_changing_session_or_owner(env, monkeypatch):
    cards, folder = env
    auth = AuthService(cards.db, cards.audit)
    raw, owner = auth.issue_market_session("synthetic-web-owner", "web-owner@example.invalid", "Old Name")
    store = BindingStore(folder / "market.json")
    store.save(owner, "synthetic-market-owner", "https://market.invalid", "synthetic-device")
    monkeypatch.setattr("backend.market_router.BindingStore", lambda: store)
    profile = {"user_id": "synthetic-market-owner", "email": "web-owner@example.invalid", "display_name": "carorfox"}
    monkeypatch.setattr(MarketClient, "profile", lambda self, token: profile)
    monkeypatch.setattr(MarketClient, "web_login", lambda self, token: {"ticket": "synthetic-once", "user": profile})

    def resolve(request):
        if not request.cookies.get("session_token"):
            raise HTTPException(401)
        return auth.resolve_session(request.cookies["session_token"])

    app = FastAPI()
    @app.exception_handler(MarketError)
    async def error(request, exc):
        return JSONResponse(status_code=exc.status, content={"detail": {"error_code": exc.code}})
    app.include_router(create_market_router(cards, resolve, "https://market.invalid", auth))
    with TestClient(app) as client:
        assert client.post("/api/market/web-login").status_code == 401
        client.cookies.set("session_token", raw)
        result = client.get("/api/market/profile")
        assert result.json() == {"display_name": "carorfox"}
        assert "set-cookie" not in result.headers
        with cards.db.session() as session:
            assert session.get(UserRow, owner).display_name == "carorfox"
        result = client.post("/api/market/web-login")
        assert result.json()["url"] == "https://market.invalid/#login=synthetic-once"
        assert "synthetic-device" not in result.text
        profile["user_id"] = "different-owner"
        assert client.get("/api/market/profile").status_code == 409
        profile["user_id"] = "synthetic-market-owner"
        profile["email"] = "different@example.invalid"
        assert client.get("/api/market/profile").status_code == 403
        store.save(owner, "synthetic-market-owner", "https://market.invalid", "synthetic-device", purpose="receive")
        assert client.post("/api/market/web-login").status_code == 403
        store.clear(owner)
        assert client.post("/api/market/web-login").status_code == 409


def test_remote_expired_authorization_does_not_expire_local_session(monkeypatch):
    def denied(*args, **kwargs):
        raise MarketError("SESSION_INVALID", 401)
    monkeypatch.setattr(MarketClient, "_request", denied)
    for action in ("profile", "web_login"):
        with pytest.raises(MarketError) as error:
            getattr(MarketClient("https://market.invalid"), action)("synthetic-device")
        assert error.value.status == 409

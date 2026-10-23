"""Market binding storage and local upload/import bridge; no network calls."""
import io
import json
import uuid

import pytest
from PIL import Image

from backend.characters.package import export_package, import_package
from backend.characters.schemas import Profile
from backend.characters.service import CharacterService
from backend.logging_audit.db import Database
from backend.logging_audit.repositories import PgAuditLog
from backend.market_link import BindingStore, MarketClient, MarketError
from backend.market_router import create_market_router
from backend.materials.storage import MaterialStorage
from backend.slice1.config_service import ConfigService
from backend.market_link import bind_start
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse


def test_register_opens_market_home_with_fragment():
    from urllib.parse import urlsplit, parse_qs
    first, second = bind_start("https://market.invalid/"), bind_start("https://market.invalid")
    url = urlsplit(first["url"])
    assert url.path == "/"
    assert not url.query
    assert parse_qs(url.fragment)["bind"] == [first["code"]]
    assert len(first["code"]) == 12
    assert first["code"] != second["code"]


@pytest.mark.parametrize("failure", [None, "auth", "storage"])
def test_binding_requires_session_and_encrypted_storage(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "synthetic-market-bridge-key")
    store = BindingStore(tmp_path / "bindings.json")
    monkeypatch.setattr("backend.market_router.BindingStore", lambda: store)
    monkeypatch.setattr(MarketClient, "bind_status", lambda self, code: {
        "status": "confirmed", "user_id": "market-synthetic",
        "email": "synthetic@example.invalid", "display_name": "Synthetic", "token": "synthetic-device",
    })

    class Auth:
        def issue_market_session(self, *args):
            if failure == "auth":
                raise MarketError("AUTH_UNAVAILABLE", 503, "Synthetic unavailable")
            return "synthetic-session", "local-synthetic"

    if failure == "storage":
        def fail_save(*args):
            raise MarketError("BINDING_INVALID", 409, "Synthetic storage failure")
        monkeypatch.setattr(store, "save", fail_save)

    app = FastAPI()
    @app.exception_handler(MarketError)
    async def controlled_error(request, error):
        return JSONResponse(status_code=error.status, content={"detail": {"error_code": error.code}})
    app.include_router(create_market_router(None, lambda request: None,
                                           base_url="https://market.invalid", auth=Auth()))
    with TestClient(app) as client:
        result = client.get("/api/market/bind/status", params={"code": "synthetic-code"})
    if failure:
        assert result.status_code == (503 if failure == "auth" else 409)
        assert "set-cookie" not in result.headers
        assert not result.json().get("bound")
    else:
        assert result.status_code == 200
        assert result.json()["user"]["user_id"] == "local-synthetic"
        assert result.json()["bound"] is True
        assert "token" not in result.json()
        assert "HttpOnly" in result.headers["set-cookie"]
        assert "Secure" not in result.headers["set-cookie"]
        assert store.get("local-synthetic")["token"] == "synthetic-device"


def test_binding_restores_real_session_and_reuses_existing_owner(env, monkeypatch):
    from backend.auth.service import AuthService
    cards, tmp_path = env
    auth = AuthService(cards.db, PgAuditLog(cards.db))
    email = f"synthetic-{uuid.uuid4().hex}@example.invalid"
    existing = auth.register(email, "synthetic-password-123", open_registration=True)
    store = BindingStore(tmp_path / "bindings.json")
    monkeypatch.setattr("backend.market_router.BindingStore", lambda: store)
    monkeypatch.setattr(MarketClient, "bind_status", lambda self, code: {
        "status": "confirmed", "user_id": "market-" + existing["user_id"],
        "email": email, "display_name": "Synthetic", "token": "synthetic-device",
    })
    app = FastAPI()
    app.include_router(create_market_router(cards, lambda request: None,
                                           base_url="https://market.invalid", auth=auth))
    with TestClient(app) as client:
        response = client.get("/api/market/bind/status", params={"code": "synthetic-code"})
        assert response.status_code == 200
        local = auth.resolve_session(client.cookies["session_token"])
        assert local.user_id == existing["user_id"]
        assert local.password_hash == ""
        assert store.get(local.user_id)["market_user_id"] == "market-" + existing["user_id"]


def image():
    output = io.BytesIO()
    Image.new("RGBA", (32, 40), (10, 30, 20, 200)).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "synthetic-market-bridge-key")
    db = Database()
    db.create_all()
    audit = PgAuditLog(db)
    configs = ConfigService(db, encryption_key="synthetic-market-bridge-key", audit=audit)
    cards = CharacterService(db, configs, audit, MaterialStorage(tmp_path))
    try:
        yield cards, tmp_path
    finally:
        db.dispose()


def test_binding_store_keeps_token_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "synthetic-market-bridge-key")
    store = BindingStore(tmp_path / "bindings.json")
    store.save("synthetic-owner", "market-user-1", "https://market.invalid", "synthetic-device-token")

    raw = (tmp_path / "bindings.json").read_text(encoding="utf-8")
    assert "synthetic-device-token" not in raw
    restored = store.get("synthetic-owner")
    assert restored["token"] == "synthetic-device-token"
    assert restored["market_user_id"] == "market-user-1"

    store.clear("synthetic-owner")
    assert store.get("synthetic-owner") is None


def test_binding_store_rejects_corrupted_token(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "synthetic-market-bridge-key")
    path = tmp_path / "bindings.json"
    path.write_text(json.dumps({"synthetic-owner": {
        "market_user_id": "m", "base_url": "https://market.invalid", "token": "not-ciphertext"}}),
        encoding="utf-8")
    with pytest.raises(MarketError) as caught:
        BindingStore(path).get("synthetic-owner")
    assert caught.value.code == "BINDING_INVALID"


def test_router_requires_binding_for_upload_and_reports_market_errors(env, monkeypatch, tmp_path):
    cards, _ = env
    monkeypatch.setattr("backend.market_router.BindingStore",
                        lambda *args, **kwargs: BindingStore(tmp_path / "bindings.json"))
    router = create_market_router(cards, lambda request: type("U", (), {"user_id": "synthetic-owner"})(),
                                  base_url="https://market.invalid")

    def route(path, method):
        for item in router.routes:
            if item.path == path and method in item.methods:
                return item
        raise AssertionError(path)

    owner = "synthetic-owner"
    card = cards.create(owner, Profile(name="Synthetic market card", tags=["synthetic"]))
    asset = cards.add_asset(owner, card["card_id"], 1, image(), "image", "image/png", "neutral", confirmed=True)
    cards.edit(owner, card["card_id"], 1, Profile(name="Synthetic market card", tags=["synthetic"],
                                                  media_ids=[asset], base_asset_id=asset))

    from backend.market_router import UploadIn
    with pytest.raises(MarketError) as caught:
        route("/api/market/upload", "POST").endpoint(
            type("R", (), {"headers": {}, "cookies": {}})(), UploadIn(card_ids=[card["card_id"]]))
    assert caught.value.code == "BIND_REQUIRED"


def test_export_package_can_be_reimported(env):
    cards, _ = env
    owner = "synthetic-roundtrip-" + uuid.uuid4().hex
    card = cards.create(owner, Profile(name="Synthetic roundtrip", tags=["synthetic"]))
    asset = cards.add_asset(owner, card["card_id"], 1, image(), "image", "image/png", "neutral", confirmed=True)
    cards.edit(owner, card["card_id"], 1, Profile(name="Synthetic roundtrip", tags=["synthetic"],
                                                  media_ids=[asset], base_asset_id=asset))
    payload = export_package(cards, owner, card["card_id"])
    other = "synthetic-other-" + uuid.uuid4().hex
    imported = import_package(cards, other, payload)
    assert imported["profile"]["name"] == "Synthetic roundtrip"
    assert imported["profile"]["media_ids"] != [asset]

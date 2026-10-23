"""Standalone market registration with isolated SQLite and synthetic email delivery."""
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def market(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "server" / "market" / "app.py"
    spec = importlib.util.spec_from_file_location("synthetic_market", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(module, "DB_PATH", tmp_path / "market.db")
    monkeypatch.setattr(module, "STORAGE", tmp_path / "cards")
    module.init_db()
    return module


def test_market_registration_can_be_consumed_before_web_login(market):
    code = "Synthetic123"
    email = "synthetic@example.invalid"
    with TestClient(market.app, base_url="https://market.invalid") as browser, TestClient(market.app) as desktop:
        page = browser.get("/bind", params={"code": code})
        assert page.status_code == 200
        with market.db() as conn:
            conn.execute("INSERT INTO email_codes VALUES(?,?,?,?,0,?)", (
                email, "", market.digest("123456"),
                market.iso(market.now() + market.CODE_TTL), market.iso(market.now())))
        registered = browser.post("/api/register", json={
            "email": email, "password": "synthetic-password",
            "display_name": "Synthetic", "code": "123456", "bind_code": code,
        })
        assert registered.status_code == 200
        confirmed = desktop.get("/api/bind/status", params={"code": code})
        assert confirmed.json()["status"] == "confirmed"
        assert desktop.get("/api/bind/status", params={"code": code}).status_code == 410
        login = browser.post("/api/login", json={"email": email, "password": "synthetic-password"})
        assert login.status_code == 200
        assert browser.get("/api/me").json()["authenticated"] is True
        # Rendering the page again must not make a consumed code reusable.
        browser.get("/bind", params={"code": code})
        assert desktop.get("/api/bind/status", params={"code": code}).status_code == 410


def test_register_still_requires_desktop_context(market):
    with TestClient(market.app) as client:
        response = client.post("/api/register", json={
            "email": "synthetic@example.invalid", "password": "synthetic-password",
            "display_name": "Synthetic", "code": "123456",
        })
        assert response.status_code == 409
        assert response.json()["detail"]["error_code"] == "LOCAL_REQUIRED"

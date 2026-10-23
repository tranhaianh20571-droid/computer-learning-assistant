"""Owned market uploads and deletion, using isolated SQLite and synthetic packages."""
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_market_registration import market  # noqa: F401 - shared market fixture


def signed_client(market, owner):
    raw = "synthetic-session-" + owner
    with market.db() as conn:
        conn.execute("INSERT INTO users VALUES(?,?,?,?,1,?)",
                     (owner, owner + "@example.invalid", "", owner, market.iso(market.now())))
        conn.execute("INSERT INTO sessions VALUES(?,?,?,?)",
                     (market.digest(raw), owner, market.iso(market.now() + market.SESSION_TTL),
                      market.iso(market.now())))
    client = TestClient(market.app, base_url="https://market.invalid")
    client.cookies.set(market.SESSION_COOKIE, raw)
    return client


def upload(client, title):
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("manifest.json", json.dumps({
            "format": "studypal.card/1", "profile": {"name": title, "identity": "Synthetic mentor"},
        }))
    response = client.post("/api/cards", data={"title": title, "tags": '["synthetic"]'},
                           files={"package": ("synthetic.zip", package.getvalue(), "application/zip")})
    assert response.status_code == 201
    return response.json()["card_id"]


def test_mine_requires_identity_and_has_direct_page(market):
    with TestClient(market.app) as anonymous:
        assert anonymous.get("/api/cards?scope=mine").status_code == 401
        assert anonymous.get("/api/cards").status_code == 200
        assert anonymous.get("/uploads").status_code == 200
        assert anonymous.delete("/api/cards/card_unknown").status_code == 401
        with market.db() as conn:
            entry = conn.execute("SELECT * FROM audit_logs").fetchone()
        assert entry["actor_id"] is None and entry["result"] == "denied"


def test_mine_filters_on_server_and_preserves_public_catalog(market):
    with signed_client(market, "synthetic-alice") as alice, signed_client(market, "synthetic-bob") as bob:
        own = upload(alice, "Synthetic Alice")
        other = upload(bob, "Synthetic Bob")
        response = alice.get("/api/cards?scope=mine&owner_id=synthetic-bob")
        assert response.headers["cache-control"] == "private, no-store"
        private = response.json()["data"]
        assert [card["card_id"] for card in private] == [own]
        assert "owner_id" not in private[0] and "email" not in private[0]
        assert {card["card_id"] for card in alice.get("/api/cards").json()["data"]} == {own, other}
        token = alice.post("/api/devices").json()["token"]
        with TestClient(market.app) as device:
            response = device.get("/api/cards?scope=mine", headers={"Authorization": "Bearer " + token})
            assert [card["card_id"] for card in response.json()["data"]] == [own]


def test_owner_delete_removes_market_assets_and_keeps_other_uploads(market):
    with signed_client(market, "synthetic-alice") as alice, signed_client(market, "synthetic-bob") as bob:
        own = upload(alice, "Synthetic Alice")
        other = upload(bob, "Synthetic Bob")
        assert bob.delete("/api/cards/" + own).status_code == 404
        assert (market.STORAGE / own / "package.zip").exists()
        assert alice.delete("/api/cards/" + own).json() == {"ok": True}
        assert not (market.STORAGE / own).exists()
        assert alice.get("/api/cards?scope=mine").json()["data"] == []
        assert [card["card_id"] for card in bob.get("/api/cards").json()["data"]] == [other]
        assert alice.get("/api/cards/" + own + "/package").status_code == 404
        with market.db() as conn:
            events = conn.execute("SELECT * FROM audit_logs ORDER BY id").fetchall()
        assert [row["result"] for row in events] == ["denied", "success"]
        assert all(row["object_id"] == own for row in events)
        for statement in ("UPDATE audit_logs SET result='changed'", "DELETE FROM audit_logs"):
            with pytest.raises(sqlite3.IntegrityError), market.db() as conn:
                conn.execute(statement)


def test_cross_origin_cookie_delete_is_rejected_and_audited(market):
    with signed_client(market, "synthetic-alice") as client:
        card = upload(client, "Synthetic Alice")
        response = client.delete("/api/cards/" + card, headers={"Origin": "https://foreign.invalid"})
        assert response.status_code == 403
        assert (market.STORAGE / card / "package.zip").exists()
        with market.db() as conn:
            assert conn.execute("SELECT result FROM audit_logs").fetchone()["result"] == "denied"


def test_cleanup_failure_remains_retryable_and_uses_controlled_error(market, monkeypatch):
    with signed_client(market, "synthetic-alice") as client:
        card = upload(client, "Synthetic Alice")
        original = Path.unlink
        def reject_package(path, *args, **kwargs):
            if path.name == "package.zip":
                raise OSError("synthetic-private-provider-content")
            return original(path, *args, **kwargs)
        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", reject_package)
            response = client.delete("/api/cards/" + card)
        assert response.status_code == 503
        assert response.json()["detail"]["error_code"] == "DELETE_FAILED"
        assert "synthetic-private" not in response.text
        assert len(client.get("/api/cards?scope=mine").json()["data"]) == 1
        assert client.delete("/api/cards/" + card).status_code == 200
        with market.db() as conn:
            assert [row["result"] for row in conn.execute("SELECT * FROM audit_logs ORDER BY id")] == ["failed", "success"]


def test_audit_failure_prevents_deleting_files(market):
    with signed_client(market, "synthetic-alice") as client:
        card = upload(client, "Synthetic Alice")
        with market.db() as conn:
            conn.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON audit_logs BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        response = client.delete("/api/cards/" + card)
        assert response.status_code == 503
        assert response.json()["detail"]["error_code"] == "AUDIT_UNAVAILABLE"
        assert (market.STORAGE / card / "package.zip").exists()
        assert len(client.get("/api/cards?scope=mine").json()["data"]) == 1

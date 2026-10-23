import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend.auth.guest import require_user
from backend.auth.service import AuthError
from backend.db import Database
from backend.slice0_app import Slice0State, create_slice0_app
from backend.logging_audit.repositories import PgAuditLog


def test_no_guest_cookie_or_header_can_create_identity():
    class Auth:
        def resolve_session(self, token):
            raise AuthError("SESSION_INVALID", "synthetic", status=401)
    for headers in ([], [(b"cookie", b"guest_id=synthetic")],
                    [(b"x-actor-id", b"other-user")], [(b"authorization", b"Bearer invalid")]):
        request = Request({"type": "http", "headers": headers})
        with pytest.raises(HTTPException) as rejected:
            require_user(request, Auth())
        assert rejected.value.status_code == 401


def test_login_cookie_restore_logout_and_cross_user_task():
    db = Database()
    audit = PgAuditLog(db)
    state = Slice0State(db, audit)
    app = create_slice0_app(state)
    email = "synthetic." + uuid.uuid4().hex[:10] + "@example.com"
    password = "Synthetic-login-password-12345"
    admin = state.auth.register(email, password, open_registration=True)
    other = state.auth.register("other." + email, password, open_registration=True)
    with TestClient(app) as client:
        assert client.get("/api/tasks/unknown/status").status_code == 401
        assert client.get("/api/auth/guest").status_code == 410
        assert client.post("/tasks", json={}).status_code == 404
        signed = client.post("/api/auth/login", json={"email": email, "password": password, "remember_me": True})
        assert signed.status_code == 200
        assert "HttpOnly" in signed.headers["set-cookie"]
        assert client.get("/api/auth/me").json()["user_id"] == admin["user_id"]
        other_task = state.tasks.create_task(owner_user_id=other["user_id"],
            kind="chat_answer", subject_id="synthetic-conversation", idempotency_key=uuid.uuid4().hex)
        denied = client.get("/api/tasks/" + other_task["task_id"] + "/status",
                            headers={"X-Actor-Id": other["user_id"]})
        assert denied.status_code == 404
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/auth/me").status_code == 401
        assert client.get("/api/tasks/unknown/status").status_code == 401
    db.dispose()


def test_public_reset_never_returns_reset_token(monkeypatch):
    from backend.auth import mail
    monkeypatch.setattr(mail, "delivery_settings", lambda: None)
    monkeypatch.setattr(mail, "send_token", lambda *args: None)
    db = Database()
    app = create_slice0_app(Slice0State(db, PgAuditLog(db)))
    with TestClient(app) as client:
        result = client.post("/api/auth/password-reset/request", json={
            "email": "synthetic.unknown@example.com"})
        assert result.status_code == 200
        assert result.json() == {"ok": True}
        invalid = client.post("/api/auth/login", json={"email": "invalid", "password": "synthetic-private"})
        assert invalid.status_code == 422
        assert "synthetic-private" not in invalid.text
    db.dispose()

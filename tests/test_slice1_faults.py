"""切片 1 故障注入与端到端验收（T08，AC-09）。

覆盖：连接器撤销、nonce 重放、能力不可用、外发确认失效、凭据泄漏扫描、跨账户访问。
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

os.environ.setdefault(
    "LEARNING_DATABASE_URL",
    "postgresql+psycopg://learning_assistant:learning_dev_pw@127.0.0.1:5432/learning_assistant",
)

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog  # noqa: E402
from backend.slice0_app import Slice0State  # noqa: E402
from backend.slice1.crypto import ENV_KEY as APP_ENCRYPTION_KEY  # noqa: E402
from backend.slice1_app import Slice1State, create_slice1_app  # noqa: E402

FAKE_KEY = "sk-test-fake-key-000000000000"
FAKE_EMAIL = "synthetic.slice1@example.com"
ENCRYPTION_KEY = "slice1-test-encryption-key"


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture(scope="module")
def client(database: Database):
    # 主密钥从环境变量读取（与生产路径一致），测试内不硬编码到应用状态。
    os.environ[APP_ENCRYPTION_KEY] = ENCRYPTION_KEY
    audit = PgAuditLog(database)
    slice0 = Slice0State(database, audit)
    slice1 = Slice1State(database, audit)
    app = create_slice1_app(slice1, slice0)
    with TestClient(app) as c:
        c.slice0_state = slice0  # type: ignore[attr-defined]
        c.slice1_state = slice1  # type: ignore[attr-defined]
        yield c


def _make_user(client: TestClient, *, admin: bool = False) -> dict:
    if admin:
        email = f"synthetic.admin.{uuid.uuid4().hex[:6]}@example.com"
        pw = "Synthetic-Admin-Passw0rd!"
        info = client.slice0_state.auth.ensure_bootstrap_admin(email, pw)  # type: ignore[attr-defined]
        from backend.auth.models import UserRow

        with client.slice0_state.db.session() as sess:  # type: ignore[attr-defined]
            row = sess.get(UserRow, info["user_id"])
            row.email = email
            row.password_hash = client.slice0_state.auth._hash_password(pw)  # type: ignore[attr-defined]
            row.status = "approved"
            row.is_admin = True
            sess.commit()
        login = client.post("/api/auth/login", json={"email": email, "password": pw})
        assert login.status_code == 200
        return {"user_id": info["user_id"], "headers": {"Authorization": f"Bearer {login.json()['session_token']}"}}

    email = f"synthetic.user.{uuid.uuid4().hex[:6]}@example.com"
    pw = "Synthetic-User-Passw0rd!"
    admin = client.slice0_state.auth.ensure_bootstrap_admin(  # type: ignore[attr-defined]
        f"synthetic.bootstrap.{uuid.uuid4().hex[:6]}@example.com", "Synthetic-Bootstrap-Passw0rd!"
    )
    reg = client.post("/api/auth/register", json={"email": email, "password": pw, "display_name": "u"})
    assert reg.status_code == 201, reg.text
    uid = reg.json()["user_id"]
    client.post("/api/auth/verify-email", json={"token": reg.json()["verify_token"]})
    # 确保 bootstrap admin 的邮箱可用（可能已存在）
    from backend.auth.models import UserRow

    with client.slice0_state.db.session() as sess:  # type: ignore[attr-defined]
        row = sess.get(UserRow, admin["user_id"])
        row.status = "approved"
        row.is_admin = True
        sess.commit()
    client.post(
        "/api/auth/admin/approve",
        json={"user_id": uid},
        headers={"Authorization": f"Bearer {_login_bootstrap(client, admin['user_id'])}"},
    )
    login = client.post("/api/auth/login", json={"email": email, "password": pw})
    assert login.status_code == 200, login.text
    return {"user_id": uid, "headers": {"Authorization": f"Bearer {login.json()['session_token']}"}}


def _login_bootstrap(client: TestClient, user_id: str) -> str:
    from backend.auth.models import UserRow

    email = f"bootstrap.{uuid.uuid4().hex[:6]}@example.com"
    pw = "Synthetic-Bootstrap-Passw0rd!"
    with client.slice0_state.db.session() as sess:  # type: ignore[attr-defined]
        row = sess.get(UserRow, user_id)
        row.email = email
        row.password_hash = client.slice0_state.auth._hash_password(pw)  # type: ignore[attr-defined]
        row.status = "approved"
        row.is_admin = True
        sess.commit()
    login = client.post("/api/auth/login", json={"email": email, "password": pw})
    return login.json()["session_token"]


def _create_config(client: TestClient, headers: dict, **overrides) -> dict:
    body = {
        "kind": "content_model",
        "protocol": "openai",
        "endpoint": "https://api.example.test/v1",
        "model_name": "m",
        "credentials": {"api_key": FAKE_KEY},
        "owner_scope": "user",
    }
    body.update(overrides)
    r = client.post("/api/capabilities/configs", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


class TestCredentialLeakScan:
    def test_api_and_db_never_expose_plaintext(self, client: TestClient):
        user = _make_user(client)
        view = _create_config(client, user["headers"])
        assert FAKE_KEY not in json.dumps(view, ensure_ascii=False)
        listed = client.get("/api/capabilities/configs", headers=user["headers"]).json()
        assert FAKE_KEY not in json.dumps(listed, ensure_ascii=False)
        # 审计记录不含明文
        audit = client.slice1_state.audit.list_all_for_test()  # type: ignore[attr-defined]
        assert FAKE_KEY not in json.dumps([r.to_record() for r in audit], ensure_ascii=False)

    def test_capability_test_does_not_leak_credentials(self, client: TestClient, monkeypatch):
        user = _make_user(client)
        view = _create_config(client, user["headers"])

        class FakeAdapter:
            capabilities = ("text",)

            def probe_all(self):
                return {"text": {"state": "available", "detail": ""}}

            def close(self):
                pass

        client.slice1_state.capabilities.adapter_factory = lambda *a, **k: FakeAdapter()  # type: ignore[attr-defined]
        r = client.post(f"/api/capabilities/configs/{view['config_id']}/test", headers=user["headers"])
        assert r.status_code == 200
        assert FAKE_KEY not in r.text


class TestCrossAccountAccess:
    def test_config_cross_account_denied(self, client: TestClient):
        user_a = _make_user(client)
        user_b = _make_user(client)
        view = _create_config(client, user_a["headers"])
        r = client.get(f"/api/capabilities/configs/{view['config_id']}", headers=user_b["headers"])
        assert r.status_code == 404
        r = client.patch(
            f"/api/capabilities/configs/{view['config_id']}",
            json={"endpoint": "https://evil.test/v1"},
            headers=user_b["headers"],
        )
        assert r.status_code == 404

    def test_disclosure_cross_account_denied(self, client: TestClient):
        user_a = _make_user(client)
        user_b = _make_user(client)
        view = _create_config(client, user_a["headers"])
        grant = client.post(
            "/api/disclosures",
            json={"task_id": "task_1", "config_id": view["config_id"], "content_category": "material"},
            headers=user_a["headers"],
        )
        assert grant.status_code == 201
        r = client.get("/api/disclosures?task_id=task_1", headers=user_b["headers"])
        assert r.json()["data"] == []
        r = client.post(f"/api/disclosures/{grant.json()['grant_id']}/revoke", headers=user_b["headers"])
        assert r.status_code == 404

    def test_connector_cross_account_denied(self, client: TestClient):
        user_a = _make_user(client)
        user_b = _make_user(client)
        pairing = client.post("/api/connectors/pairing", json={"device_name": "pc"}, headers=user_a["headers"])
        binding = client.post(
            "/api/connectors/bind", json={"pairing_code": pairing.json()["pairing_code"]}
        ).json()
        r = client.post(f"/api/connectors/{binding['binding_id']}/revoke", headers=user_b["headers"])
        assert r.status_code == 404


class TestDisclosureFaults:
    def test_revoked_disclosure_blocks_worker(self, client: TestClient):
        user = _make_user(client)
        view = _create_config(client, user["headers"])
        grant = client.post(
            "/api/disclosures",
            json={"task_id": "task_f1", "config_id": view["config_id"], "content_category": "query"},
            headers=user["headers"],
        ).json()
        client.post(f"/api/disclosures/{grant['grant_id']}/revoke", headers=user["headers"])
        disclosures = client.slice1_state.disclosures  # type: ignore[attr-defined]
        from backend.slice1.errors import Slice1Error

        with pytest.raises(Slice1Error) as exc:
            disclosures.verify(task_id="task_f1", config_id=view["config_id"], content_category="query")
        assert exc.value.code == "disclosure_revoked"

    def test_config_change_invalidates_grant(self, client: TestClient):
        user = _make_user(client)
        view = _create_config(client, user["headers"])
        client.post(
            "/api/disclosures",
            json={"task_id": "task_f2", "config_id": view["config_id"], "content_category": "material"},
            headers=user["headers"],
        )
        client.patch(
            f"/api/capabilities/configs/{view['config_id']}",
            json={"endpoint": "https://api2.example.test/v1"},
            headers=user["headers"],
        )
        disclosures = client.slice1_state.disclosures  # type: ignore[attr-defined]
        from backend.slice1.errors import Slice1Error

        with pytest.raises(Slice1Error):
            disclosures.verify(task_id="task_f2", config_id=view["config_id"], content_category="material")


class TestConnectorFaults:
    def test_nonce_replay_rejected(self, client: TestClient):
        user = _make_user(client)
        pairing = client.post("/api/connectors/pairing", json={"device_name": "pc"}, headers=user["headers"]).json()
        binding = client.post(
            "/api/connectors/bind", json={"pairing_code": pairing["pairing_code"]}
        ).json()
        req = client.post(
            "/api/connectors/requests",
            json={"binding_id": binding["binding_id"], "task_id": "task_n1"},
            headers=user["headers"],
        ).json()
        first = client.post(
            "/api/connectors/requests/accept",
            json={"binding_id": binding["binding_id"], "task_id": "task_n1", "nonce": req["nonce"]},
            headers={"X-Connector-Token": binding["binding_token"]},
        )
        assert first.status_code == 200
        replay = client.post(
            "/api/connectors/requests/accept",
            json={"binding_id": binding["binding_id"], "task_id": "task_n1", "nonce": req["nonce"]},
            headers={"X-Connector-Token": binding["binding_token"]},
        )
        assert replay.status_code == 409
        assert replay.json()["error_code"] == "nonce_replay"

    def test_revoke_rejects_new_requests(self, client: TestClient):
        user = _make_user(client)
        pairing = client.post("/api/connectors/pairing", json={"device_name": "pc"}, headers=user["headers"]).json()
        binding = client.post(
            "/api/connectors/bind", json={"pairing_code": pairing["pairing_code"]}
        ).json()
        client.post(f"/api/connectors/{binding['binding_id']}/revoke", headers=user["headers"])
        r = client.post(
            "/api/connectors/requests",
            json={"binding_id": binding["binding_id"], "task_id": "task_n2"},
            headers=user["headers"],
        )
        assert r.status_code == 403
        assert r.json()["error_code"] == "connector_revoked"

    def test_pairing_code_single_use_over_http(self, client: TestClient):
        user = _make_user(client)
        pairing = client.post("/api/connectors/pairing", json={"device_name": "pc"}, headers=user["headers"]).json()
        first = client.post("/api/connectors/bind", json={"pairing_code": pairing["pairing_code"]})
        assert first.status_code == 201
        second = client.post("/api/connectors/bind", json={"pairing_code": pairing["pairing_code"]})
        assert second.status_code == 400
        assert second.json()["error_code"] == "pairing_code_invalid"


class TestCapabilityUnavailable:
    def test_unsupported_capability_maps_to_controlled_error(self):
        from backend.slice1.adapters import require_capability
        from backend.slice1.errors import Slice1Error

        with pytest.raises(Slice1Error) as exc:
            require_capability({"json_schema": {"state": "unavailable"}}, "json_schema")
        assert exc.value.code == "capability_unavailable"
        assert "model_capability_unavailable" in exc.value.message
        # 支持时不抛
        require_capability({"json_schema": {"state": "available"}}, "json_schema")

    def test_no_infinite_retry_marker(self):
        """能力不可用时返回受控错误码，调用方据此停止而不是重试。"""
        from backend.slice1.adapters import require_capability
        from backend.slice1.errors import Slice1Error

        for _ in range(3):
            with pytest.raises(Slice1Error) as exc:
                require_capability({"tool_call": {"state": "unavailable"}}, "tool_call")
            assert exc.value.code == "capability_unavailable"


class TestAdminConfigOverHTTP:
    def test_admin_config_invisible_to_user(self, client: TestClient):
        admin = _make_user(client, admin=True)
        user = _make_user(client)
        admin_cfg = _create_config(
            client,
            admin["headers"],
            kind="ocr",
            protocol="paddleocr",
            endpoint="https://ocr.example.test/api",
            model_name="PaddleOCR-VL-1.6",
            owner_scope="admin",
        )
        listed = client.get("/api/capabilities/configs", headers=user["headers"]).json()["data"]
        assert all(c["config_id"] != admin_cfg["config_id"] for c in listed)
        r = client.get(f"/api/capabilities/configs/{admin_cfg['config_id']}", headers=user["headers"])
        assert r.status_code == 404

"""切片 1：本机连接器配对、围栏、nonce 防重放与撤销（T06，AC-06/AC-07）。"""

from __future__ import annotations

import os
import sys
import threading
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

os.environ.setdefault(
    "LEARNING_DATABASE_URL",
    "postgresql+psycopg://learning_assistant:learning_dev_pw@127.0.0.1:5432/learning_assistant",
)

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog  # noqa: E402
from backend.slice1.connector.gateway import ConnectorGateway, offline_state  # noqa: E402
from backend.slice1.connector_service import (  # noqa: E402
    ConnectorService,
    is_loopback_target,
    validate_loopback_target,
)
from backend.slice1.errors import Slice1Error  # noqa: E402
from backend.slice1.models import ConnectorBindingRow, ConnectorRequestRow, utcnow  # noqa: E402


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture()
def connectors(database: Database) -> ConnectorService:
    return ConnectorService(database, PgAuditLog(database))


def _bound(connectors: ConnectorService) -> tuple[dict, dict]:
    owner = f"user_{uuid.uuid4().hex[:8]}"
    pairing = connectors.create_pairing(actor_id=owner, device_name="pc")
    binding = connectors.bind(pairing_code=pairing["pairing_code"], device_name="pc")
    return binding, {"owner": owner, "pairing": pairing}


class TestPairing:
    def test_pairing_code_single_use(self, connectors: ConnectorService):
        pairing = connectors.create_pairing(actor_id="user_a", device_name="pc")
        first = connectors.bind(pairing_code=pairing["pairing_code"])
        assert first["status"] == "bound"
        with pytest.raises(Slice1Error) as exc:
            connectors.bind(pairing_code=pairing["pairing_code"])
        assert exc.value.code == "pairing_code_invalid"

    def test_pairing_code_expires(self, connectors: ConnectorService, database: Database):
        pairing = connectors.create_pairing(actor_id="user_a", device_name="pc")
        with database.session() as sess:
            row = sess.get(ConnectorBindingRow, pairing["binding_id"])
            row.pairing_expires_at = utcnow() - timedelta(seconds=1)
            sess.commit()
        with pytest.raises(Slice1Error) as exc:
            connectors.bind(pairing_code=pairing["pairing_code"])
        assert exc.value.code == "pairing_code_invalid"

    def test_invalid_code_rejected(self, connectors: ConnectorService):
        with pytest.raises(Slice1Error) as exc:
            connectors.bind(pairing_code="not-a-real-code")
        assert exc.value.code == "pairing_code_invalid"

    def test_binding_token_resolves_owner(self, connectors: ConnectorService):
        binding, ctx = _bound(connectors)
        resolved = connectors.resolve_binding(binding["binding_token"])
        assert resolved["owner_user_id"] == ctx["owner"]


class TestRequestFencing:
    def test_only_bound_account_task_accepted(self, connectors: ConnectorService):
        binding, ctx = _bound(connectors)
        request = connectors.enqueue_request(
            binding_id=binding["binding_id"], task_id="task_1", call_type="model.generate"
        )
        accepted = connectors.accept_request(
            binding_id=binding["binding_id"],
            task_id="task_1",
            nonce=request["nonce"],
            owner_user_id=ctx["owner"],
            task_owner_user_id=ctx["owner"],
        )
        assert accepted["status"] == "delivered"

    def test_other_account_task_rejected(self, connectors: ConnectorService):
        binding, ctx = _bound(connectors)
        request = connectors.enqueue_request(binding_id=binding["binding_id"], task_id="task_2")
        with pytest.raises(Slice1Error) as exc:
            connectors.accept_request(
                binding_id=binding["binding_id"],
                task_id="task_2",
                nonce=request["nonce"],
                owner_user_id=ctx["owner"],
                task_owner_user_id="someone_else",
            )
        assert exc.value.code == "access_denied"

    def test_expired_request_rejected(self, connectors: ConnectorService, database: Database):
        binding, ctx = _bound(connectors)
        request = connectors.enqueue_request(binding_id=binding["binding_id"], task_id="task_3", ttl_seconds=1)
        with database.session() as sess:
            row = sess.get(ConnectorRequestRow, request["request_id"])
            row.expires_at = utcnow() - timedelta(seconds=1)
            sess.commit()
        with pytest.raises(Slice1Error) as exc:
            connectors.accept_request(
                binding_id=binding["binding_id"],
                task_id="task_3",
                nonce=request["nonce"],
                owner_user_id=ctx["owner"],
            )
        assert exc.value.code == "request_expired"

    def test_nonce_replay_rejected(self, connectors: ConnectorService):
        binding, ctx = _bound(connectors)
        request = connectors.enqueue_request(binding_id=binding["binding_id"], task_id="task_4")
        connectors.accept_request(
            binding_id=binding["binding_id"],
            task_id="task_4",
            nonce=request["nonce"],
            owner_user_id=ctx["owner"],
        )
        with pytest.raises(Slice1Error) as exc:
            connectors.accept_request(
                binding_id=binding["binding_id"],
                task_id="task_4",
                nonce=request["nonce"],
                owner_user_id=ctx["owner"],
            )
        assert exc.value.code == "nonce_replay"

    def test_call_type_limited(self, connectors: ConnectorService):
        binding, _ = _bound(connectors)
        with pytest.raises(Slice1Error) as exc:
            connectors.enqueue_request(
                binding_id=binding["binding_id"], task_id="task_5", call_type="shell.exec"
            )
        assert exc.value.code == "invalid_field"

    def test_request_size_limited(self, connectors: ConnectorService):
        binding, _ = _bound(connectors)
        with pytest.raises(Slice1Error) as exc:
            connectors.enqueue_request(
                binding_id=binding["binding_id"],
                task_id="task_6",
                payload={"blob": "x" * (70 * 1024)},
            )
        assert exc.value.code == "invalid_field"


class TestRevocationAndOffline:
    def test_revoke_rejects_new_and_inflight(self, connectors: ConnectorService):
        binding, ctx = _bound(connectors)
        request = connectors.enqueue_request(binding_id=binding["binding_id"], task_id="task_7")
        connectors.revoke(binding["binding_id"], actor_id=ctx["owner"])
        # 在途请求被标记 rejected
        with connectors.db.session() as sess:
            row = sess.get(ConnectorRequestRow, request["request_id"])
            assert row.status == "rejected"
        # 新请求拒绝
        with pytest.raises(Slice1Error) as exc:
            connectors.enqueue_request(binding_id=binding["binding_id"], task_id="task_8")
        assert exc.value.code == "connector_revoked"
        # 重放也拒绝
        with pytest.raises(Slice1Error) as exc:
            connectors.accept_request(
                binding_id=binding["binding_id"],
                task_id="task_7",
                nonce=request["nonce"],
                owner_user_id=ctx["owner"],
            )
        assert exc.value.code == "connector_revoked"

    def test_revoked_token_rejected(self, connectors: ConnectorService):
        binding, ctx = _bound(connectors)
        connectors.revoke(binding["binding_id"], actor_id=ctx["owner"])
        with pytest.raises(Slice1Error) as exc:
            connectors.resolve_binding(binding["binding_token"])
        assert exc.value.code == "connector_revoked"

    def test_offline_state_visible(self, connectors: ConnectorService):
        binding, ctx = _bound(connectors)
        assert offline_state(connectors, binding["binding_id"]) == "offline"
        connectors.mark_seen(binding["binding_id"])
        assert offline_state(connectors, binding["binding_id"]) == "online"
        connectors.revoke(binding["binding_id"], actor_id=ctx["owner"])
        assert offline_state(connectors, binding["binding_id"]) == "revoked"

    def test_cross_account_revoke_denied(self, connectors: ConnectorService):
        binding, ctx = _bound(connectors)
        with pytest.raises(Slice1Error) as exc:
            connectors.revoke(binding["binding_id"], actor_id="intruder")
        assert exc.value.code == "binding_not_found"


class TestLoopbackTarget:
    def test_loopback_allowed(self):
        for url in ("http://127.0.0.1:11434", "http://[::1]:8000", "http://localhost:1234"):
            assert is_loopback_target(url)
            validate_loopback_target(url)

    def test_non_loopback_rejected(self):
        for url in ("http://10.0.0.5:8000", "https://example.com", "http://192.168.1.10:80"):
            assert not is_loopback_target(url)
            with pytest.raises(Slice1Error) as exc:
                validate_loopback_target(url)
            assert exc.value.code == "invalid_target"


class TestWSSGateway:
    def test_bind_then_accept_over_wss(self, connectors: ConnectorService):
        from websockets.sync.client import connect

        gateway = ConnectorGateway(connectors, host="127.0.0.1", port=0)
        url = gateway.start()
        try:
            pairing = connectors.create_pairing(actor_id="user_ws", device_name="pc")
            with connect(url, max_size=64 * 1024) as ws:
                ws.send('{"type":"bind","pairing_code":"%s","device_name":"pc"}' % pairing["pairing_code"])
                import json as _json

                bound = _json.loads(ws.recv())
                assert bound["type"] == "bound"
                binding_id = bound["binding_id"]
                assert gateway.is_connected(binding_id)

                # 连接器向服务端提交请求（带任务与 nonce），服务端围栏核验
                request = connectors.enqueue_request(binding_id=binding_id, task_id="task_ws")
                ws.send(_json.dumps({"type": "request", "task_id": "task_ws", "nonce": request["nonce"]}))
                accepted = _json.loads(ws.recv())
                assert accepted["type"] == "accepted"
                assert accepted["task_id"] == "task_ws"
        finally:
            gateway.stop()

    def test_revoked_binding_cannot_rebind_over_wss(self, connectors: ConnectorService):
        from websockets.sync.client import connect

        gateway = ConnectorGateway(connectors, host="127.0.0.1", port=0)
        url = gateway.start()
        try:
            pairing = connectors.create_pairing(actor_id="user_ws2", device_name="pc")
            binding = connectors.bind(pairing_code=pairing["pairing_code"])
            connectors.revoke(binding["binding_id"], actor_id="user_ws2")
            with connect(url, max_size=64 * 1024) as ws:
                import json as _json

                ws.send(_json.dumps({"type": "auth", "binding_token": binding["binding_token"]}))
                reply = _json.loads(ws.recv())
                assert reply["type"] == "error"
                assert reply["error_code"] == "connector_revoked"
        finally:
            gateway.stop()

    def test_offline_marked_on_disconnect(self, connectors: ConnectorService):
        from websockets.sync.client import connect

        gateway = ConnectorGateway(connectors, host="127.0.0.1", port=0)
        url = gateway.start()
        try:
            pairing = connectors.create_pairing(actor_id="user_ws3", device_name="pc")
            with connect(url, max_size=64 * 1024) as ws:
                import json as _json

                ws.send(_json.dumps({"type": "bind", "pairing_code": pairing["pairing_code"]}))
                bound = _json.loads(ws.recv())
                binding_id = bound["binding_id"]
                assert gateway.is_connected(binding_id)
            # 连接关闭后网关不再持有连接
            import time

            time.sleep(0.2)
            assert not gateway.is_connected(binding_id)
        finally:
            gateway.stop()

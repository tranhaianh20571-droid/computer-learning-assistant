"""切片 1：能力配置加密、掩码、权限与版本（T01/T02，AC-01）。"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.db import get_database_url  # noqa: E402

os.environ.setdefault("LEARNING_DATABASE_URL", get_database_url())

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgAuditLog  # noqa: E402
from backend.slice1.config_service import ConfigService  # noqa: E402
from backend.slice1.errors import Slice1Error  # noqa: E402
from backend.slice1.models import ServiceConfigRow  # noqa: E402
from sqlalchemy import text  # noqa: E402

FAKE_KEY = "sk-test-fake-key-000000000000"
TEST_ENCRYPTION_KEY = "test-encryption-key-material"


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture()
def configs(database: Database) -> ConfigService:
    return ConfigService(database, encryption_key=TEST_ENCRYPTION_KEY)


def _owner() -> str:
    return f"user_{uuid.uuid4().hex[:8]}"


class TestCredentialEncryption:
    def test_db_has_no_plaintext(self, database: Database, configs: ConfigService):
        owner = _owner()
        view = configs.create(
            actor_id=owner,
            is_admin=False,
            kind="content_model",
            protocol="openai",
            endpoint="https://api.example.test/v1",
            model_name="gpt-test",
            credentials={"api_key": FAKE_KEY},
        )
        with database.session() as sess:
            row = sess.get(ServiceConfigRow, view["config_id"])
            assert FAKE_KEY not in row.encrypted_credentials
        # 原始字节扫描：整表不含明文密钥
        with database.engine.connect() as conn:
            blob = conn.execute(
                text("SELECT encrypted_credentials FROM service_configs WHERE config_id = :c"),
                {"c": view["config_id"]},
            ).scalar_one()
        assert FAKE_KEY not in blob

    def test_api_view_only_mask(self, configs: ConfigService):
        view = configs.create(
            actor_id=_owner(),
            is_admin=False,
            kind="content_model",
            protocol="openai",
            endpoint="https://api.example.test/v1",
            model_name="m",
            credentials={"api_key": FAKE_KEY},
        )
        text_view = json.dumps(view, ensure_ascii=False)
        assert FAKE_KEY not in text_view
        assert "****" in view["credential_mask"]
        assert "encrypted_credentials" not in view
        assert "credentials" not in view

    def test_decrypt_roundtrip_for_worker(self, configs: ConfigService):
        view = configs.create(
            actor_id=_owner(),
            is_admin=False,
            kind="content_model",
            protocol="openai",
            endpoint="https://api.example.test/v1",
            model_name="m",
            credentials={"api_key": FAKE_KEY},
        )
        assert configs.resolve_credentials(view["config_id"])["api_key"] == FAKE_KEY

    def test_wrong_key_cannot_decrypt(self, database: Database, configs: ConfigService):
        view = configs.create(
            actor_id=_owner(),
            is_admin=False,
            kind="content_model",
            protocol="openai",
            endpoint="https://api.example.test/v1",
            model_name="m",
            credentials={"api_key": FAKE_KEY},
        )
        other = ConfigService(database, encryption_key="different-key")
        with pytest.raises(Exception):
            other.resolve_credentials(view["config_id"])

    def test_encryption_key_from_env(self, database: Database, monkeypatch):
        monkeypatch.setenv("APP_ENCRYPTION_KEY", TEST_ENCRYPTION_KEY)
        svc = ConfigService(database)
        view = svc.create(
            actor_id=_owner(),
            is_admin=False,
            kind="content_model",
            protocol="openai",
            endpoint="https://api.example.test/v1",
            model_name="m",
            credentials={"api_key": FAKE_KEY},
        )
        assert svc.resolve_credentials(view["config_id"])["api_key"] == FAKE_KEY


class TestConfigVersioning:
    def test_update_credentials_bumps_version(self, configs: ConfigService):
        owner = _owner()
        v = configs.create(
            actor_id=owner,
            is_admin=False,
            kind="content_model",
            protocol="openai",
            endpoint="https://api.example.test/v1",
            model_name="m",
            credentials={"api_key": FAKE_KEY},
        )
        assert v["config_version"] == 1
        updated = configs.update(
            v["config_id"],
            actor_id=owner,
            is_admin=False,
            credentials={"api_key": "sk-another-fake-key-1111"},
        )
        assert updated["config_version"] == 2
        assert updated["capability_status"]["text"] == "unknown"

    def test_deactivate_blocks_worker(self, configs: ConfigService):
        owner = _owner()
        v = configs.create(
            actor_id=owner,
            is_admin=False,
            kind="content_model",
            protocol="openai",
            endpoint="https://api.example.test/v1",
            model_name="m",
            credentials={"api_key": FAKE_KEY},
        )
        configs.deactivate(v["config_id"], actor_id=owner, is_admin=False)
        with pytest.raises(Slice1Error) as exc:
            configs.active_config_for_worker(v["config_id"])
        assert exc.value.code == "config_not_found"


class TestConfigAccessControl:
    def test_cross_account_denied(self, configs: ConfigService):
        owner = _owner()
        v = configs.create(
            actor_id=owner,
            is_admin=False,
            kind="content_model",
            protocol="openai",
            endpoint="https://api.example.test/v1",
            model_name="m",
            credentials={"api_key": FAKE_KEY},
        )
        with pytest.raises(Slice1Error) as exc:
            configs.get(v["config_id"], actor_id="intruder", is_admin=False)
        assert exc.value.code == "config_not_found"

    def test_admin_config_invisible_to_user(self, configs: ConfigService):
        admin_cfg = configs.create(
            actor_id="admin_1",
            is_admin=True,
            kind="ocr",
            protocol="paddleocr",
            endpoint="https://ocr.example.test/api",
            model_name="PaddleOCR-VL-1.6",
            credentials={"api_key": FAKE_KEY},
            owner_scope="admin",
        )
        assert configs.list(actor_id="user_1", is_admin=False) == []
        with pytest.raises(Slice1Error) as exc:
            configs.get(admin_cfg["config_id"], actor_id="user_1", is_admin=False)
        assert exc.value.code == "config_not_found"
        assert configs.get(admin_cfg["config_id"], actor_id="admin_1", is_admin=True)

    def test_user_cannot_create_admin_config(self, configs: ConfigService):
        with pytest.raises(Slice1Error) as exc:
            configs.create(
                actor_id="user_1",
                is_admin=False,
                kind="search",
                protocol="tavily_hikari",
                endpoint="https://search.example.test",
                model_name="",
                credentials={"api_key": FAKE_KEY},
                owner_scope="admin",
            )
        assert exc.value.code == "admin_only"

    def test_protocol_kind_mismatch_rejected(self, configs: ConfigService):
        with pytest.raises(Slice1Error) as exc:
            configs.create(
                actor_id=_owner(),
                is_admin=False,
                kind="tts",
                protocol="openai",
                endpoint="https://api.example.test/v1",
                model_name="m",
                credentials={"api_key": FAKE_KEY},
            )
        assert exc.value.code == "protocol_mismatch"


class TestMigrations:
    def test_slice1_tables_exist(self, database: Database):
        with database.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            ).scalars().all()
        for table in ("service_configs", "connector_bindings", "disclosure_grants", "connector_requests"):
            assert table in rows

    def test_migration_chain_reversible(self):
        versions = list((ROOT / "server" / "backend" / "app" / "alembic" / "versions").glob("*.py"))
        text_all = "\n".join(v.read_text(encoding="utf-8") for v in versions)
        assert "b1c2d3e4f5a6" in text_all
        migration = [v for v in versions if "b1c2d3e4f5a6" in v.name][0].read_text(encoding="utf-8")
        assert "def downgrade" in migration
        assert "down_revision = \"a42e8c1f0d9a\"" in migration

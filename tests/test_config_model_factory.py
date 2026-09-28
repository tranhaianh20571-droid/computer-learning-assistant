"""配置驱动模型工厂：latest_active_for_owner 选择与协议映射（设置页为真相源）。"""

from __future__ import annotations

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
from backend.slice1.config_service import ConfigService  # noqa: E402
from backend.slice3_app import _config_model_factory  # noqa: E402

FAKE_KEY = "sk-test-fake-key-000000000000"
TEST_ENCRYPTION_KEY = "test-encryption-key-material"


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    return db


@pytest.fixture()
def configs(database: Database) -> ConfigService:
    return ConfigService(database, encryption_key=TEST_ENCRYPTION_KEY)


def _owner() -> str:
    return f"user_{uuid.uuid4().hex[:8]}"


def _create(configs: ConfigService, owner: str, protocol: str, endpoint: str, model: str = "m") -> dict:
    return configs.create(
        actor_id=owner,
        is_admin=False,
        kind="content_model",
        protocol=protocol,
        endpoint=endpoint,
        model_name=model,
        credentials={"api_key": FAKE_KEY},
    )


class TestLatestActiveForOwner:
    def test_returns_none_without_config(self, configs: ConfigService):
        assert configs.latest_active_for_owner(_owner(), "content_model") is None

    def test_skips_other_owner_and_kind(self, configs: ConfigService):
        owner = _owner()
        _create(configs, owner, "openai", "https://a.example.test/v1")
        # 其他属主、其他 kind 不串
        assert configs.latest_active_for_owner(_owner(), "content_model") is None
        assert configs.latest_active_for_owner(owner, "tts") is None

    def test_latest_wins_and_falls_back_after_deactivate(self, configs: ConfigService):
        owner = _owner()
        first = _create(configs, owner, "openai", "https://old.example.test/v1", "old")
        second = _create(configs, owner, "openai", "https://new.example.test/v1", "new")
        got = configs.latest_active_for_owner(owner, "content_model")
        assert got is not None and got["config_id"] == second["config_id"]
        configs.deactivate(second["config_id"], actor_id=owner, is_admin=False)
        got = configs.latest_active_for_owner(owner, "content_model")
        assert got is not None and got["config_id"] == first["config_id"]


class TestConfigModelFactory:
    def test_no_config_returns_none(self, configs: ConfigService):
        factory = _config_model_factory(configs)
        assert factory(_owner(), "task_x") is None

    def test_openai_endpoint_kept(self, configs: ConfigService):
        owner = _owner()
        _create(configs, owner, "openai", "https://api.example.test/v1", "gpt-x")
        adapter = _config_model_factory(configs)(owner, "task_x")
        assert type(adapter).__name__ == "OpenAIAdapter"
        assert adapter.endpoint == "https://api.example.test/v1"
        assert adapter.model_name == "gpt-x"

    def test_gemini_strips_v1_and_decrypts_credentials(self, configs: ConfigService):
        owner = _owner()
        _create(configs, owner, "gemini", "https://gw.example.test/v1", "gemini-x")
        adapter = _config_model_factory(configs)(owner, "task_x")
        assert type(adapter).__name__ == "GeminiAdapter"
        assert adapter.endpoint == "https://gw.example.test"
        assert adapter.model_name == "gemini-x"
        assert adapter.credentials["api_key"] == FAKE_KEY

    def test_anthropic_strips_v1(self, configs: ConfigService):
        owner = _owner()
        _create(configs, owner, "anthropic", "https://api.example.test/v1", "claude-x")
        adapter = _config_model_factory(configs)(owner, "task_x")
        assert type(adapter).__name__ == "AnthropicAdapter"
        assert adapter.endpoint == "https://api.example.test"
        assert adapter.model_name == "claude-x"

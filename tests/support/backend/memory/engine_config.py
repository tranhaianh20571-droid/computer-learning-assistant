"""Deployment-only Hindsight settings. Native providers perform memory work."""
import hashlib
import os
from pathlib import Path
import tempfile
import threading
from typing import Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select, text

from ..slice1.errors import Slice1Error, error
from ..slice1.model_catalog import discover_models, normalize_base
from .models import MemorySpaceRow

LOCK = threading.RLock()
DEFAULT_PATH = Path(__file__).resolve().parents[3] / ".env.memory"
PREFIXES = {
    "content": "HINDSIGHT_API_LLM",
    "embedding": "HINDSIGHT_API_EMBEDDINGS_OPENAI",
    "jev": "HINDSIGHT_API_RERANKER_TYPESAFE",
    "rerank": "HINDSIGHT_API_RERANKER_SILICONFLOW",
}


def config_path():
    return Path(os.environ.get("MEMORY_CONFIG_FILE", DEFAULT_PATH))


def revision(path):
    return hashlib.sha256(path.read_bytes() if path.is_file() else b"").hexdigest()


def values():
    return {k: v for k, v in dotenv_values(config_path()).items() if v is not None}


class ProviderIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    endpoint: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = Field(default=None, repr=False, max_length=4096)


class EngineIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: str = Field(min_length=64, max_length=64)
    content: ProviderIn
    embedding: ProviderIn
    # Retained for one release so older clients can still save; the value is ignored.
    dimensions: int | None = Field(default=None, exclude=True)
    rerank_protocol: Literal["jev", "rerank"]
    rerank: ProviderIn


class EngineModelsIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: Literal["content", "embedding", "rerank"]
    protocol: Literal["jev", "rerank"] = "rerank"
    endpoint: str = Field(min_length=1, max_length=500)
    api_key: SecretStr | None = Field(default=None, repr=False, max_length=4096)


def public_settings():
    path, config = config_path(), values()
    protocol = "jev" if config.get("HINDSIGHT_API_RERANKER_PROVIDER") == "typesafe" else "rerank"
    result = {}
    for kind, prefix in (("content", PREFIXES["content"]), ("embedding", PREFIXES["embedding"]),
                         ("rerank", PREFIXES[protocol])):
        result[kind] = {"endpoint": config.get(prefix + "_BASE_URL", ""),
                        "model": config.get(prefix + "_MODEL", ""),
                        "credential_mask": "********" if config.get(prefix + "_API_KEY") else ""}
    current = revision(path)
    applied = path.with_name(path.name + ".applied")
    active = applied.read_text().strip() if applied.is_file() else ""
    result.update(revision=current, dimensions=int(config.get("HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS", "1024")),
                  rerank_protocol=protocol, status="active" if current == active else "restart_required")
    return result


def save_settings(body, db, audit, actor):
    with LOCK, db.session() as locked:
        if locked.bind.dialect.name == "postgresql":
            locked.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 4810261018})
        path, config = config_path(), values()
        if body.expected_revision != revision(path):
            raise Slice1Error("invalid_field", "配置已变化，请重新加载。", 409)
        embedding_base = normalize_base(body.embedding.endpoint, allow_local=True)
        previous = (config.get(PREFIXES["embedding"] + "_MODEL", ""),
                    config.get(PREFIXES["embedding"] + "_BASE_URL", "").rstrip("/"))
        proposed = (body.embedding.model.strip(), embedding_base)
        if previous != proposed:
            with db.session() as session:
                if session.scalar(select(MemorySpaceRow.space_id).where(
                        MemorySpaceRow.status != "deleted").limit(1)):
                    raise Slice1Error("invalid_field", "已有记忆，切换 Embedding 需先完成独立迁移。", 409)
        for kind, provider, prefix in (
            ("content", body.content, PREFIXES["content"]),
            ("embedding", body.embedding, PREFIXES["embedding"]),
            (body.rerank_protocol, body.rerank, PREFIXES[body.rerank_protocol]),
        ):
            endpoint = normalize_base(provider.endpoint, kind, allow_local=True)
            old_endpoint = config.get(prefix + "_BASE_URL", "")
            key = provider.api_key.get_secret_value().strip() if provider.api_key else ""
            if not key and old_endpoint and normalize_base(old_endpoint, kind, allow_local=True) != endpoint:
                raise error("credential_invalid", "地址已变化，请重新填写对应 API Key。")
            key = key or config.get(prefix + "_API_KEY", "")
            if not key or "\n" in key or "\r" in key or len(key) > 4096:
                raise error("credential_invalid", "请填写对应 API Key。")
            model = provider.model.strip()
            if not model or not model.isprintable():
                raise error("invalid_field", "模型名无效。")
            config.update({prefix + "_BASE_URL": endpoint, prefix + "_MODEL": model,
                           prefix + "_API_KEY": key})
        config.update(HINDSIGHT_API_LLM_PROVIDER="openai",
            HINDSIGHT_API_EMBEDDINGS_PROVIDER="openai",
            HINDSIGHT_API_RERANKER_PROVIDER="typesafe" if body.rerank_protocol == "jev" else "siliconflow")
        # Embedding storage width belongs to the existing Hindsight schema; keep it.
        config.setdefault("HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS", "2000")
        audit.append("config.changed", actor_id=actor, admin_id=actor,
            object_type="memory_engine", object_id="deployment", result="save_requested")
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf8", dir=path.parent,
                                             prefix=".env.memory.", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                for name, value in sorted(config.items()):
                    # dotenv single-quoted strings preserve literal URL and credential bytes.
                    stream.write(name + "='" + value.replace("\\", "\\\\").replace("'", "\\'") + "'\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError:
            audit.append("config.changed", actor_id=actor, admin_id=actor,
                object_type="memory_engine", object_id="deployment", result="save_failed")
            raise error("invalid_field", "配置未保存，请检查服务端文件权限。") from None
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        audit.append("config.changed", actor_id=actor, admin_id=actor,
            object_type="memory_engine", object_id="deployment", result="saved")
        return public_settings()


def models(body):
    config = values()
    kind = body.protocol if body.kind == "rerank" else body.kind
    prefix = PREFIXES[kind]
    key = body.api_key.get_secret_value().strip() if body.api_key else ""
    base = normalize_base(body.endpoint, kind, allow_local=True)
    if not key:
        saved = config.get(prefix + "_BASE_URL", "")
        if not saved or normalize_base(saved, kind, allow_local=True) != base:
            raise error("credential_invalid", "地址已变化，请填写 API Key。")
        key = config.get(prefix + "_API_KEY", "")
    if not key:
        raise error("credential_invalid", "请填写 API Key。")
    return discover_models(base, "jev" if kind == "jev" else "openai", key, allow_local=True)

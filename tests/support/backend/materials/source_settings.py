"""Personal reader connections with encrypted credentials and frozen profiles."""
from __future__ import annotations

import json
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select

from .source_models import SourceProfileRow
from .source_clients import validate_service_endpoint
from ..slice1.errors import Slice1Error, error

DEFAULTS = {
    "firecrawl": {"enabled": True, "timeout": 90, "wait_ms": 0, "keep_images": True},
    "bilinote": {"enabled": True, "screenshot": True, "link": True, "video_understanding": True,
                 "interval": 6, "grid_size": 3, "max_frames": 60, "asr_model": "small"},
}


class ReaderOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    enabled: bool = True
    timeout: int = Field(default=90, ge=5, le=120)
    wait_ms: int = Field(default=0, ge=0, le=10000)
    keep_images: bool = True
    screenshot: bool = True
    link: bool = True
    video_understanding: bool = True
    interval: int = Field(default=6, ge=1, le=120)
    grid_size: int = Field(default=3, ge=1, le=4)
    max_frames: int = Field(default=60, ge=1, le=600)
    asr_model: str = Field(default="small", pattern=r"^(tiny|base|small|medium|large-v3)$")


class ReaderConfigIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(ge=0)
    endpoint: str = Field(min_length=1, max_length=500)
    api_key: SecretStr | None = None
    options: ReaderOptions = Field(default_factory=ReaderOptions)


class SourceSettings:
    def __init__(self, configs):
        self.configs, self.db = configs, configs.db

    def get(self, owner, provider):
        if provider not in DEFAULTS:
            raise error("invalid_field")
        with self.db.session() as session:
            row = session.get(SourceProfileRow, (owner, provider))
            config_id = row.config_id if row else None
            options = {**DEFAULTS[provider], **json.loads(row.options_json if row else "{}")}
            revision = row.revision if row else 0
        view = self.configs.get(config_id, actor_id=owner, is_admin=False) if config_id else None
        return {"provider": provider, "revision": revision, "configuration": view, "options": options}

    def save(self, owner, provider, body):
        if provider not in DEFAULTS:
            raise error("invalid_field")
        endpoint = body.endpoint.strip().rstrip("/")
        validate_service_endpoint(endpoint, provider)
        with self.db.session() as session:
            if session.bind.dialect.name == "postgresql":
                from sqlalchemy import func
                session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("source-settings:" + owner, 0))))
            row = session.get(SourceProfileRow, (owner, provider), with_for_update=True)
            if (row.revision if row else 0) != body.expected_revision:
                raise Slice1Error("invalid_field", "配置已变化，请重新加载。", 409)
            from ..slice1.models import ServiceConfigRow
            current = session.get(ServiceConfigRow, row.config_id, with_for_update=True) if row and row.config_id else None
            key = body.api_key.get_secret_value().strip() if body.api_key else ""
            if not key and current:
                if current.owner_user_id != owner or not current.is_active:
                    raise error("config_not_found")
                if current.endpoint != endpoint:
                    raise error("credential_invalid", "更换服务地址后请重新填写密钥。")
                key = self.configs._decrypt(current.encrypted_credentials).get("api_key", "")
            if provider == "bilinote" and endpoint == "builtin://bilinote":
                key = "builtin"
            if not key or len(key) > 4096 or any(ord(c) < 32 for c in key):
                raise error("credential_invalid")
            # Copy on write: old tasks retain the exact saved connection.
            view = self.configs.create(actor_id=owner, is_admin=False,
                kind="web_reader" if provider == "firecrawl" else "video_reader",
                protocol=provider, endpoint=endpoint, model_name="",
                credentials={"api_key": key}, session=session)
            if row is None:
                row = SourceProfileRow(owner_user_id=owner, provider=provider, revision=0)
                session.add(row)
            row.config_id, row.revision = view["config_id"], row.revision + 1
            valid_keys = DEFAULTS[provider].keys()
            row.options_json = json.dumps({k: v for k, v in body.options.model_dump().items() if k in valid_keys})
            self.configs.audit.append("config.changed", actor_id=owner, object_type="source_profile",
                object_id=provider, result="saved", session=session)
        return self.get(owner, provider)

    def snapshot(self, owner, provider):
        profile = self.get(owner, provider)
        view = profile["configuration"]
        if not view or not view["is_active"] or not profile["options"]["enabled"]:
            raise error("config_not_found", "请先保存并启用对应的资料解析配置。")
        return {"config_id": view["config_id"], "config_version": view["config_version"],
                "endpoint": view["endpoint"], "protocol": provider,
                "options": profile["options"], "revision": profile["revision"]}

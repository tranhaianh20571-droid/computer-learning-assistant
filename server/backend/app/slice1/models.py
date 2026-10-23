"""切片 1 数据模型：能力配置、连接器绑定、外发授权、连接器请求。

对应交接文档 §3.1 核心契约。所有个人资源带 `owner_user_id`；
管理员云能力配置 `owner_scope="admin"` 且 `owner_user_id` 为 NULL。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, Computed, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from ..auth.models import UserRow  # noqa: F401 - owner FK metadata
from ..logging_audit.models import Base

CONFIG_KINDS = ("content_model", "tts", "ocr", "search", "image_generation", "video_generation", "document_parser", "web_reader", "video_reader")
PROTOCOLS = ("openai_image", "openai", "openai_responses", "anthropic", "gemini", "minimax", "paddleocr", "anysearch", "minimax_video", "seedance", "somark", "paddleocr_vl", "firecrawl", "bilinote")
CAPABILITIES = ("text", "image", "tool_call", "json_schema", "streaming", "cancel")
CAPABILITY_STATES = ("unknown", "available", "unavailable")
CONTENT_CATEGORIES = ("material", "query", "generated_text", "image", "audio_text", "character_media")

# 协议 → 允许的配置类型
PROTOCOL_KIND = {
    "firecrawl": "web_reader",
    "bilinote": "video_reader",
    "openai_responses": "content_model",
    "paddleocr_vl": "document_parser",
    "somark": "document_parser",
    "minimax_video": "video_generation",
    "seedance": "video_generation",
    "openai_image": "image_generation",
    "openai": "content_model",
    "anthropic": "content_model",
    "gemini": "content_model",
    "minimax": "tts",
    "paddleocr": "ocr",
    "anysearch": "search",
}

# 各协议默认探测能力集
PROTOCOL_CAPABILITIES = {
    "firecrawl": ("connection",),
    "bilinote": ("connection",),
    "openai_responses": CAPABILITIES,
    "paddleocr_vl": ("connection",),
    "somark": ("connection",),
    "minimax_video": ("video_generate",),
    "seedance": ("video_generate",),
    "openai_image": ("image_generate",),
    "openai": CAPABILITIES,
    "anthropic": CAPABILITIES,
    "gemini": CAPABILITIES,
    "minimax": ("text",),
    "paddleocr": ("text", "image"),
    "anysearch": ("text",),
}

ADMIN_KINDS = ("ocr",)
USER_KINDS = ("content_model", "tts", "image_generation", "video_generation", "search", "document_parser", "web_reader", "video_reader")
SOMARK_ENDPOINT = "https://somark.cn/api/v1"
PADDLE_VL_ENDPOINT = "https://paddleocr.aistudio-app.com/api/v2/ocr/jobs"
PADDLE_VL_MODEL = "PaddleOCR-VL-1.6"
DOCUMENT_DESTINATIONS = {"somark": (SOMARK_ENDPOINT, ""),
                         "paddleocr_vl": (PADDLE_VL_ENDPOINT, PADDLE_VL_MODEL)}
ANYSEARCH_ENDPOINT = "https://api.anysearch.com"
ACTIVE_SEARCH_PREDICATE = "owner_scope = 'user' AND kind = 'search' AND protocol = 'anysearch' AND is_active"
ACTIVE_SEARCH_OWNER_EXPRESSION = "CASE WHEN kind = 'search' AND protocol = 'anysearch' AND is_active THEN owner_user_id ELSE NULL END"
ACTIVE_SEARCH_OWNER_CHECK = "NOT (kind = 'search' AND protocol = 'anysearch' AND is_active) OR (owner_scope = 'user' AND owner_user_id IS NOT NULL AND owner_user_id <> '')"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def empty_capability_status(protocol: str) -> dict:
    return {cap: "unknown" for cap in PROTOCOL_CAPABILITIES.get(protocol, ("text",))}


class SearchConfigOwnerRow(Base):
    """Serialization row for a trusted search owner, not an auth user or grant."""

    __tablename__ = "search_config_owners"
    __table_args__ = (
        CheckConstraint("owner_user_id <> ''", name="ck_search_config_owners_nonempty"),
    )

    owner_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)


class ServiceConfigRow(Base):
    __tablename__ = "service_configs"
    __table_args__ = (
        Index("idx_service_configs_owner", "owner_user_id"),
        Index("idx_service_configs_kind", "owner_scope", "kind", "is_active"),
        Index("uq_service_configs_personal_search", "owner_user_id", unique=True,
              postgresql_where=text(ACTIVE_SEARCH_PREDICATE), sqlite_where=text(ACTIVE_SEARCH_PREDICATE)),
        CheckConstraint(ACTIVE_SEARCH_OWNER_CHECK, name="ck_service_configs_search_owner"),
    )

    config_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(64))
    # A generated FK enforces search ownership without rewriting legacy owners
    # or imposing a new FK on unrelated capability configurations.
    active_search_owner_id: Mapped[str | None] = mapped_column(
        String(64), Computed(ACTIVE_SEARCH_OWNER_EXPRESSION, persisted=True),
        ForeignKey("search_config_owners.owner_user_id", name="fk_service_configs_search_owner"),
    )
    search_test_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    owner_scope: Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    protocol: Mapped[str] = mapped_column(String(30), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    model_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False, default="")
    credential_mask: Mapped[str] = mapped_column(String(64), nullable=False, default="****")
    capability_status: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    reasoning_settings: Mapped[str] = mapped_column(Text, nullable=False,
        default='{"mode":"default","show_summary":true}', server_default='{"mode":"default","show_summary":true}')
    reasoning_validation: Mapped[str] = mapped_column(Text, nullable=False, default="{}", server_default="{}")
    reasoning_test_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    def status_dict(self) -> dict:
        try:
            return json.loads(self.capability_status or "{}")
        except json.JSONDecodeError:
            return {}


class ConnectorBindingRow(Base):
    __tablename__ = "connector_bindings"
    __table_args__ = (
        Index("idx_connector_bindings_owner", "owner_user_id"),
        Index("idx_connector_bindings_status", "status"),
    )

    binding_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    device_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    binding_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    pairing_code_hash: Mapped[str | None] = mapped_column(String(128))
    pairing_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending | bound | revoked
    bound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class DisclosureGrantRow(Base):
    __tablename__ = "disclosure_grants"
    __table_args__ = (
        Index("idx_disclosure_grants_owner", "owner_user_id"),
        Index("idx_disclosure_grants_task", "task_id"),
        Index("idx_disclosure_grants_config", "config_id"),
    )

    grant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    config_id: Mapped[str] = mapped_column(String(64), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    service_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    service_endpoint: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    content_category: Mapped[str] = mapped_column(String(30), nullable=False)
    scope_snapshot: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parent_grant_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("disclosure_grants.grant_id", ondelete="CASCADE"), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConnectorRequestRow(Base):
    __tablename__ = "connector_requests"
    __table_args__ = (
        Index("idx_connector_requests_binding", "binding_id"),
        Index("idx_connector_requests_task", "task_id"),
    )

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    binding_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(String(64), nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending | delivered | completed | rejected
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

"""能力配置 CRUD、凭据加密与掩码（切片 1 T02）。

安全边界：
- 创建时加密凭据，数据库中不出现明文。
- 查询/列表只返回掩码、协议、模型名和能力状态，永不返回完整凭据。
- 普通用户不可读取或修改管理员 OCR 配置；搜索始终属于本人。
- 更新凭据或端点生成新 config_version；停用后 worker 拒绝调用。
"""

from __future__ import annotations

import json
import re
import uuid
import unicodedata
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..auth.models import UserRow

from ..logging_audit.db import Database
from ..logging_audit.audit import AuditWriteError
from ..logging_audit.repositories import PgAuditLog
from .crypto import CredentialCryptoError, decrypt, encrypt, load_key, mask_credentials
from .errors import Slice1Error, error
from .reasoning import (DEFAULT_SETTINGS_JSON, RULES_VERSION, describe_reasoning,
                        stored_settings, validate_settings)
from .models import (
    ADMIN_KINDS,
    ANYSEARCH_ENDPOINT,
    SOMARK_ENDPOINT,
    DOCUMENT_DESTINATIONS,
    PROTOCOL_KIND,
    PROTOCOLS,
    PROTOCOL_CAPABILITIES,
    USER_KINDS,
    ServiceConfigRow,
    SearchConfigOwnerRow,
    DisclosureGrantRow,
    empty_capability_status,
    utcnow,
)


_UNSET = object()
SEARCH_TEST_INTERVAL_SECONDS = 30


class AnySearchCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    api_key: SecretStr = Field(repr=False)

    @field_validator("api_key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        text = value.get_secret_value()
        if (not text or text != text.strip() or len(text) > 4096
                or "*" in text or "•" in text
                or any(unicodedata.category(c) in ("Cc", "Cs") for c in text)):
            raise ValueError("invalid_api_key")
        return value


class SearchConfigCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    kind: Literal["search"] = "search"
    protocol: Literal["anysearch"] = "anysearch"
    owner_scope: Literal["user"] = "user"
    endpoint: Literal["https://api.anysearch.com"] = ANYSEARCH_ENDPOINT
    model_name: Literal[""] = ""
    credentials: AnySearchCredentials


class SearchConfigUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)
    endpoint: Literal["https://api.anysearch.com"] | None = None
    model_name: Literal[""] | None = None
    credentials: AnySearchCredentials | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_and_empty(cls, value):
        if not isinstance(value, dict) or not value or any(v is None for v in value.values()):
            raise ValueError("invalid_search_update")
        return value


def _search_credentials(credentials) -> dict:
    try:
        parsed = AnySearchCredentials.model_validate(credentials)
    except ValidationError:
        raise Slice1Error("invalid_field", "invalid_search_credentials", 422) from None
    return {"api_key": parsed.api_key.get_secret_value()}


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class ConfigService:
    def __init__(
        self,
        db: Database,
        *,
        encryption_key: Optional[str] = None,
        on_config_changed: Optional[Callable[[str, str, str], None]] = None,
        audit: Optional[PgAuditLog] = None,
    ) -> None:
        self.db = db
        self.audit = audit if audit is not None else PgAuditLog(db)
        self._explicit_key = encryption_key
        self._key_cache: Optional[bytes] = None
        # 配置变更回调（T05 用于失效外发授权）
        self._on_config_changed = on_config_changed

    def _search_audit(self, session, config_id: str, actor_id: str, result: str, *, is_admin=False) -> None:
        try:
            self.audit.append("config.changed", actor_id=actor_id, object_type="service_config",
                object_id=config_id, result=result, admin_id=actor_id if is_admin else None,
                session=session)
        except AuditWriteError:
            raise
        except Exception:
            raise AuditWriteError("storage_failure") from None

    def _search_change(self, sess, row, actor_id: str, action: str, *, is_admin=False) -> None:
        if row.kind in ("content_model", "video_generation"):
            self._search_audit(sess, row.config_id, actor_id, action, is_admin=is_admin)
            return
        if row.kind not in ("search", "document_parser", "web_reader", "video_reader"):
            return
        if action != "created":
            grants = sess.scalars(select(DisclosureGrantRow).where(
                DisclosureGrantRow.config_id == row.config_id,
                DisclosureGrantRow.revoked_at.is_(None)).with_for_update()).all()
            for grant in grants:
                grant.revoked_at = utcnow()
        self._search_audit(sess, row.config_id, actor_id, action, is_admin=is_admin)

    def record_search_probe_failure(self, config_id: str, *, actor_id: str, is_admin=False) -> None:
        with self.db.session() as sess:
            self._search_audit(sess, config_id, actor_id, "probe_failed", is_admin=is_admin)

    @property
    def _key_material(self) -> bytes:
        # 懒加载：缺少 APP_ENCRYPTION_KEY 时应用仍可启动，只在凭据操作时报受控错误。
        if self._key_cache is None:
            try:
                self._key_cache = load_key(self._explicit_key)
            except CredentialCryptoError:
                raise error(
                    "credential_invalid",
                    "服务器尚未配置凭据加密密钥，请联系管理员设置 APP_ENCRYPTION_KEY 并重启服务后重试。",
                ) from None
        return self._key_cache

    # ---- 内部：加密/解密 ----
    def _encrypt(self, credentials: dict) -> str:
        return encrypt(json.dumps(credentials, ensure_ascii=False, sort_keys=True), self._key_material)

    def _decrypt(self, ciphertext: str) -> dict:
        if not ciphertext:
            return {}
        try:
            raw = decrypt(ciphertext, self._key_material)
        except CredentialCryptoError:
            raise error(
                "credential_invalid",
                "无法解密已保存的服务凭据。服务器加密密钥可能已变更，或凭据已损坏；请联系管理员确认原 APP_ENCRYPTION_KEY，或在配置中重新填写并保存 API Key。",
            ) from None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise error("credential_invalid", "stored credential malformed") from exc

    def resolve_credentials(self, config_id: str) -> dict:
        """仅供 worker/适配器内部使用；API 层禁止调用。"""
        with self.db.session() as sess:
            row = sess.get(ServiceConfigRow, config_id)
            if row is None:
                raise error("config_not_found")
            if not row.is_active:
                raise error("config_not_found", "config inactive")
            if row.kind == "search":
                raise error("config_not_found", "search_owner_version_required")
            return self._decrypt(row.encrypted_credentials)

    def resolve_document_credentials(self, config_id: str, *, owner_user_id: str, expected_version: int) -> dict:
        with self.db.session() as sess:
            row = self._load(sess, config_id)
            self._authorize(row, owner_user_id, is_admin=False, write=False)
            if (row.kind != "document_parser" or row.protocol not in DOCUMENT_DESTINATIONS
                    or row.owner_scope != "user" or not row.is_active
                    or row.config_version != expected_version):
                raise error("disclosure_revoked", "document configuration changed")
            return self._decrypt(row.encrypted_credentials)

    def catalog_snapshot(self, config_id: str, *, actor_id: str, is_admin: bool):
        """Read destination and credential from one authorized configuration row."""
        with self.db.session() as sess:
            row = self._load(sess, config_id)
            self._authorize(row, actor_id, is_admin=is_admin, write=False)
            if not row.is_active or row.kind == "search":
                raise error("config_not_found")
            return self.public_view(row), self._decrypt(row.encrypted_credentials)

    def latest_active_for_owner(self, owner_user_id: str, kind: str) -> Optional[dict]:
        """适配器工厂用：属主某类最新活跃配置（元数据，不含凭据；仅 user 作用域）。"""
        with self.db.session() as sess:
            row = (
                sess.execute(
                    select(ServiceConfigRow)
                    .where(
                        ServiceConfigRow.is_active.is_(True),
                        ServiceConfigRow.kind == kind,
                        ServiceConfigRow.owner_user_id == owner_user_id,
                    )
                    .order_by(ServiceConfigRow.created_at.desc(), ServiceConfigRow.config_id.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            return self.public_view(row) if row else None

    def active_search_for_owner(self, owner_user_id: str) -> Optional[dict]:
        """Exactly one personal AnySearch config; no admin/other-provider fallback."""
        with self.db.session() as sess:
            rows = sess.scalars(select(ServiceConfigRow).where(
                ServiceConfigRow.owner_scope == "user",
                ServiceConfigRow.owner_user_id == owner_user_id,
                ServiceConfigRow.kind == "search",
                ServiceConfigRow.protocol == "anysearch",
                ServiceConfigRow.is_active.is_(True),
            ).limit(2)).all()
            if len(rows) > 1:
                raise Slice1Error("invalid_field", "ambiguous_search_config", 409)
            return self.worker_view(rows[0]) if rows else None

    @staticmethod
    def worker_view(row: ServiceConfigRow) -> dict:
        return {name: getattr(row, name) for name in (
            "config_id", "config_version", "owner_scope", "owner_user_id", "kind",
            "protocol", "endpoint", "model_name", "is_active",
        )} | {"capability_status": row.status_dict()} | ConfigService._reasoning_view(row)

    @staticmethod
    def _reasoning_view(row: ServiceConfigRow) -> dict:
        if row.kind != "content_model":
            return {}
        settings = stored_settings(row.reasoning_settings)
        try:
            caps = describe_reasoning(row.protocol, row.endpoint, row.model_name)
        except Slice1Error:
            # Legacy endpoints/names remain readable and use provider defaults.
            caps = dict(schema_version=1, rules_version=RULES_VERSION, source="unknown",
                control_kind="unknown", supported_modes=["default"], effort_values=[], budget=None,
                summary_supported=False, endpoint_scope="unverified", default_behavior="provider_default")
        try:
            verification = json.loads(row.reasoning_validation or "{}")
        except (ValueError, TypeError):
            verification = {}
        if (not isinstance(verification, dict) or verification.get("config_version") != row.config_version
                or verification.get("rules_version") != RULES_VERSION or verification.get("settings") != settings
                or not row.is_active):
            verification = {"evidence": "unknown"}
        caps["verification"] = verification
        return {"reasoning_settings": settings, "reasoning_capabilities": caps}

    @staticmethod
    def _check_search_row(row, owner_user_id: str, expected_version: int) -> None:
        if (row is None or not row.is_active or row.owner_scope != "user"
                or row.owner_user_id != owner_user_id or row.kind != "search"
                or row.protocol != "anysearch" or row.endpoint != ANYSEARCH_ENDPOINT
                or row.model_name != "" or type(expected_version) is not int
                or row.config_version != expected_version):
            raise error("config_not_found", "search_snapshot_invalid")

    def check_search_config(self, config_id: str, *, owner_user_id: str, expected_version: int) -> dict:
        with self.db.session() as sess:
            row = sess.get(ServiceConfigRow, config_id)
            self._check_search_row(row, owner_user_id, expected_version)
            return self.worker_view(row)

    def resolve_search_credentials(self, config_id: str, *, owner_user_id: str, expected_version: int) -> dict:
        """Ownership/version checks happen under the same lock BEFORE decrypting."""
        with self.db.session() as sess:
            row = sess.get(ServiceConfigRow, config_id, with_for_update=True)
            self._check_search_row(row, owner_user_id, expected_version)
            return _search_credentials(self._decrypt(row.encrypted_credentials))

    @staticmethod
    def _lock_search_owner(sess, actor_id: str, *, create: bool = False) -> None:
        # actor_id is supplied by the server's session/guest resolver, never a DTO.
        # Guest identities do not create login accounts or share their settings.
        if create:
            if (re.fullmatch(r"guest_[0-9a-f]{32}", actor_id) is None
                    and sess.get(UserRow, actor_id) is None):
                raise Slice1Error("invalid_field", "invalid_search_owner", 422)
            insert = sqlite_insert if sess.bind.dialect.name == "sqlite" else pg_insert
            sess.execute(insert(SearchConfigOwnerRow).values(owner_user_id=actor_id)
                .on_conflict_do_nothing(index_elements=["owner_user_id"]))
        owner = sess.get(SearchConfigOwnerRow, actor_id, with_for_update=True)
        if owner is None:
            raise error("config_not_found")

    def reserve_search_probe(self, config_id: str, *, actor_id: str, is_admin: bool,
                             expected_version: int) -> dict:
        """Persist owner/config cooling before I/O; key rotation cannot reset it."""
        with self.db.session() as sess:
            row = self._load(sess, config_id)
            self._authorize(row, actor_id, is_admin=is_admin, write=True)
            self._lock_search_owner(sess, actor_id)
            row = sess.get(ServiceConfigRow, config_id, with_for_update=True, populate_existing=True)
            self._check_search_row(row, actor_id, expected_version)
            recent = sess.scalar(select(func.max(ServiceConfigRow.search_test_requested_at)).where(
                ServiceConfigRow.owner_scope == "user", ServiceConfigRow.owner_user_id == actor_id,
                ServiceConfigRow.kind == "search", ServiceConfigRow.protocol == "anysearch"))
            now = utcnow()
            if recent is not None:
                if recent.tzinfo is None:
                    recent = recent.replace(tzinfo=timezone.utc)
                if (now - recent).total_seconds() < SEARCH_TEST_INTERVAL_SECONDS:
                    raise Slice1Error("capability_test_failed", "search_test_rate_limited", 429)
            row.search_test_requested_at = now
            self._search_audit(sess, config_id, actor_id, "probe_started", is_admin=is_admin)
            sess.flush()
            return self.worker_view(row)

    @staticmethod
    def _check_reasoning_row(row, actor_id: str, expected_version: int) -> None:
        if (row is None or not row.is_active or row.owner_scope != "user"
                or row.owner_user_id != actor_id or row.kind != "content_model"
                or row.protocol not in ("openai", "openai_responses", "anthropic", "gemini")):
            raise error("config_not_found")
        if type(expected_version) is not int or row.config_version != expected_version:
            raise Slice1Error("invalid_field", "reasoning_config_stale", 409)

    def reserve_reasoning_probe(self, config_id: str, *, actor_id: str, is_admin: bool,
                                expected_version: int) -> dict:
        """Reserve once under a per-owner lock before any credential read or I/O."""
        try:
            with self.db.session() as sess:
                if sess.bind.dialect.name == "postgresql":
                    sess.execute(select(func.pg_advisory_xact_lock(func.hashtextextended("reasoning:" + actor_id, 0))))
                row = self._load(sess, config_id)
                self._check_reasoning_row(row, actor_id, expected_version)
                validate_settings(stored_settings(row.reasoning_settings), protocol=row.protocol,
                    endpoint=row.endpoint, model_name=row.model_name)
                recent = sess.scalar(select(func.max(ServiceConfigRow.reasoning_test_requested_at)).where(
                    ServiceConfigRow.owner_scope == "user", ServiceConfigRow.owner_user_id == actor_id,
                    ServiceConfigRow.kind == "content_model"))
                now = utcnow()
                if recent is not None:
                    recent = recent.replace(tzinfo=timezone.utc) if recent.tzinfo is None else recent
                    if (now - recent).total_seconds() < 30:
                        raise Slice1Error("capability_test_failed", "reasoning_test_rate_limited", 429)
                row.reasoning_test_requested_at = now
                self._search_audit(sess, config_id, actor_id, "probe_started", is_admin=is_admin)
                sess.flush()
                return self.worker_view(row)
        except Slice1Error:
            with self.db.session() as sess:
                self._search_audit(sess, config_id, actor_id, "probe_failed", is_admin=is_admin)
            raise

    def check_reasoning_probe(self, config_id: str, *, actor_id: str, expected_version: int) -> None:
        with self.db.session() as sess:
            self._check_reasoning_row(sess.get(ServiceConfigRow, config_id), actor_id, expected_version)

    def resolve_reasoning_credentials(self, config_id: str, *, actor_id: str, expected_version: int) -> dict:
        with self.db.session() as sess:
            row = sess.get(ServiceConfigRow, config_id, with_for_update=True)
            self._check_reasoning_row(row, actor_id, expected_version)
            return self._decrypt(row.encrypted_credentials)

    def finish_reasoning_probe(self, config_id: str, *, actor_id: str, expected_version: int, result: dict) -> dict:
        with self.db.session() as sess:
            row = sess.get(ServiceConfigRow, config_id, with_for_update=True)
            try:
                self._check_reasoning_row(row, actor_id, expected_version)
            except Slice1Error:
                self._search_audit(sess, config_id, actor_id, "probe_stale")
                return {"evidence": "unknown", "error_code": "reasoning_config_stale",
                        "config_version": expected_version, "rules_version": RULES_VERSION}
            verification = dict(result, config_version=expected_version, rules_version=RULES_VERSION,
                settings=stored_settings(row.reasoning_settings), verified_at=_iso(utcnow()))
            row.reasoning_validation = json.dumps(verification, separators=(",", ":"))
            self._search_audit(sess, config_id, actor_id,
                "probe_completed" if result["evidence"] == "request_accepted" else "probe_failed")
            sess.flush()
            return verification

    # ---- 序列化 ----
    @staticmethod
    def public_view(row: ServiceConfigRow) -> dict:
        return {
            "config_id": row.config_id,
            "owner_scope": row.owner_scope,
            "kind": row.kind,
            "protocol": row.protocol,
            "endpoint": row.endpoint,
            "model_name": row.model_name,
            "credential_mask": "****" if row.kind == "search" else row.credential_mask,
            "capability_status": row.status_dict(),
            "config_version": row.config_version,
            "is_active": row.is_active,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        } | ConfigService._reasoning_view(row)

    # ---- 权限 ----
    @staticmethod
    def _is_admin_config(row: ServiceConfigRow) -> bool:
        return row.owner_scope == "admin"

    def _load(self, sess, config_id: str) -> ServiceConfigRow:
        row = sess.get(ServiceConfigRow, config_id, with_for_update=True)
        if row is None:
            raise error("config_not_found")
        return row

    def _authorize(self, row: ServiceConfigRow, actor_id: str, *, is_admin: bool, write: bool) -> None:
        if row.kind in ("search", "document_parser"):
            if row.owner_scope != "user" or row.owner_user_id != actor_id:
                self.audit.append("access.denied", actor_id=actor_id, object_type="service_config",
                    object_id=row.config_id, result="denied", reason="config_not_found",
                    admin_id=actor_id if is_admin else None)
                raise error("config_not_found")
            return
        if self._is_admin_config(row):
            if not is_admin:
                # 普通用户不可见管理员配置（用 not_found 避免存在性泄露）
                raise error("config_not_found")
            return
        if row.owner_user_id != actor_id:
            raise error("config_not_found")

    # ---- 创建 ----
    def create(
        self,
        *,
        actor_id: str,
        is_admin: bool,
        kind: str,
        protocol: str,
        endpoint: str,
        model_name: str,
        credentials: dict,
        owner_scope: str = "user",
        reasoning_settings: dict | object = _UNSET,
        session=None,
    ) -> dict:
        if kind == "document_parser":
            if DOCUMENT_DESTINATIONS.get(protocol) != (endpoint, model_name) or owner_scope != "user":
                raise Slice1Error("invalid_field", "invalid_document_parser_config", 422)
            credentials = _search_credentials(credentials)
        if reasoning_settings is None:
            raise Slice1Error("invalid_field", "invalid_reasoning_settings", 422)
        if kind != "content_model" and reasoning_settings is not _UNSET:
            raise Slice1Error("invalid_field", "reasoning_content_model_only", 422)
        reasoning = (validate_settings(None if reasoning_settings is _UNSET else reasoning_settings,
            protocol=protocol, endpoint=endpoint, model_name=model_name) if kind == "content_model" else None)
        if kind == "search":
            try:
                SearchConfigCreateIn.model_validate(dict(kind=kind, protocol=protocol, endpoint=endpoint,
                    model_name=model_name, credentials=credentials, owner_scope=owner_scope))
            except ValidationError:
                raise Slice1Error("invalid_field", "invalid_search_config", 422) from None
            credentials = _search_credentials(credentials)
        if protocol not in PROTOCOLS:
            raise error("protocol_mismatch", f"unknown protocol {protocol}")
        if PROTOCOL_KIND[protocol] != kind:
            raise error("protocol_mismatch", f"{protocol} is not a {kind} protocol")
        if kind in ADMIN_KINDS:
            if owner_scope != "admin":
                raise error("admin_only", "admin cloud configs must use owner_scope=admin")
            if not is_admin:
                raise error("admin_only")
        elif kind in USER_KINDS:
            if owner_scope != "user":
                raise error("invalid_field", "user configs must use owner_scope=user")
        else:
            raise error("invalid_field", f"unknown kind {kind}")
        if not endpoint:
            raise error("invalid_field", "endpoint required")
        if not credentials:
            raise error("credential_invalid", "credentials required")
        if kind in ("web_reader", "video_reader"):
            from ..materials.source_clients import validate_service_endpoint
            validate_service_endpoint(endpoint, protocol)

        if kind == "video_generation":
            from .adapters.video import validate_video_config
            from .errors import AdapterError
            try:
                validate_video_config(endpoint, model_name, credentials)
            except AdapterError as exc:
                raise error("invalid_field", exc.code) from None

        config_id = f"cfg_{uuid.uuid4().hex[:12]}"
        status = empty_capability_status(protocol)
        with (nullcontext(session) if session is not None else self.db.session()) as sess:
            if kind == "search":
                self._lock_search_owner(sess, actor_id, create=True)
                existing = sess.scalar(select(ServiceConfigRow.config_id).where(
                    ServiceConfigRow.owner_scope == "user", ServiceConfigRow.owner_user_id == actor_id,
                    ServiceConfigRow.kind == "search", ServiceConfigRow.protocol == "anysearch",
                    ServiceConfigRow.is_active.is_(True)))
                if existing is not None:
                    raise Slice1Error("invalid_field", "active_search_config_exists", 409)
            row = ServiceConfigRow(
                config_id=config_id,
                owner_user_id=None if owner_scope == "admin" else actor_id,
                owner_scope=owner_scope,
                kind=kind,
                protocol=protocol,
                endpoint=endpoint,
                model_name=model_name,
                encrypted_credentials=self._encrypt(credentials),
                credential_mask="****" if kind == "search" else mask_credentials(credentials),
                capability_status=json.dumps(status),
                reasoning_settings=json.dumps(reasoning, separators=(",", ":")) if reasoning else DEFAULT_SETTINGS_JSON,
                reasoning_validation="{}",
                config_version=1,
                is_active=True,
            )
            sess.add(row)
            try:
                sess.flush()
            except IntegrityError:
                if kind == "search":
                    raise Slice1Error("invalid_field", "active_search_config_exists", 409) from None
                raise
            self._search_change(sess, row, actor_id, "created", is_admin=is_admin)
            view = self.public_view(row)
            if session is None:
                sess.commit()
        if session is None:
            self._notify(config_id, actor_id, "created")
        return view

    def list(
        self,
        *,
        actor_id: str,
        is_admin: bool,
        kind: Optional[str] = None,
    ) -> list[dict]:
        with self.db.session() as sess:
            visibility = ((ServiceConfigRow.owner_scope == "user") & (ServiceConfigRow.owner_user_id == actor_id))
            if is_admin:
                visibility = or_(visibility, (ServiceConfigRow.owner_scope == "admin") & (ServiceConfigRow.kind != "search"))
            stmt = select(ServiceConfigRow).where(ServiceConfigRow.is_active.is_(True), visibility)
            if kind:
                stmt = stmt.where(ServiceConfigRow.kind == kind)
            rows = sess.execute(stmt.order_by(ServiceConfigRow.created_at)).scalars().all()
            visible = []
            for row in rows:
                try:
                    self._authorize(row, actor_id, is_admin=is_admin, write=False)
                except Slice1Error:
                    continue
                visible.append(self.public_view(row))
            return visible

    def get(self, config_id: str, *, actor_id: str, is_admin: bool) -> dict:
        with self.db.session() as sess:
            row = self._load(sess, config_id)
            self._authorize(row, actor_id, is_admin=is_admin, write=False)
            return self.public_view(row)

    def update(
        self,
        config_id: str,
        *,
        actor_id: str,
        is_admin: bool,
        endpoint: Optional[str] = None,
        model_name: Optional[str] = None,
        credentials: dict | None | object = _UNSET,
        reasoning_settings: dict | object = _UNSET,
    ) -> dict:
        if reasoning_settings is None:
            raise Slice1Error("invalid_field", "invalid_reasoning_settings", 422)
        if endpoint is None and model_name is None and credentials is _UNSET and reasoning_settings is _UNSET:
            raise error("invalid_field", "no fields to update")
        with self.db.session() as sess:
            row = self._load(sess, config_id)
            self._authorize(row, actor_id, is_admin=is_admin, write=True)
            if row.kind == "document_parser":
                if DOCUMENT_DESTINATIONS.get(row.protocol) != (
                        row.endpoint if endpoint is None else endpoint,
                        row.model_name if model_name is None else model_name):
                    raise Slice1Error("invalid_field", "invalid_document_parser_config", 422)
                if credentials is not _UNSET:
                    credentials = _search_credentials(credentials)
            if row.kind != "content_model" and reasoning_settings is not _UNSET:
                raise Slice1Error("invalid_field", "reasoning_content_model_only", 422)
            if row.kind == "video_generation":
                from .adapters.video import validate_video_config
                from .errors import AdapterError
                try:
                    if credentials is None:
                        raise AdapterError("credential_invalid", status=400)
                    validate_video_config(row.endpoint if endpoint is None else endpoint,
                        row.model_name if model_name is None else model_name,
                        None if credentials is _UNSET else credentials)
                except AdapterError as exc:
                    raise error("invalid_field", exc.code) from None
            reasoning = None
            if row.kind == "content_model":
                reasoning = validate_settings(stored_settings(row.reasoning_settings)
                    if reasoning_settings is _UNSET else reasoning_settings, protocol=row.protocol,
                    endpoint=row.endpoint if endpoint is None else endpoint,
                    model_name=row.model_name if model_name is None else model_name)
            if row.kind == "search":
                if row.protocol != "anysearch" or not row.is_active:
                    raise error("config_not_found")
                if (endpoint is not None and endpoint != ANYSEARCH_ENDPOINT
                        or model_name is not None and model_name != ""):
                    raise Slice1Error("invalid_field", "invalid_search_config", 422)
                if credentials is not _UNSET:
                    credentials = _search_credentials(credentials)
            changed = False
            if reasoning is not None and reasoning != stored_settings(row.reasoning_settings):
                row.reasoning_settings = json.dumps(reasoning, separators=(",", ":"))
                changed = True
            if endpoint is not None and endpoint != row.endpoint:
                if not endpoint:
                    raise error("invalid_field", "endpoint cannot be empty")
                if row.kind in ("web_reader", "video_reader"):
                    from ..materials.source_clients import validate_service_endpoint
                    validate_service_endpoint(endpoint, row.protocol)
                row.endpoint = endpoint
                changed = True
            if model_name is not None and model_name != row.model_name:
                row.model_name = model_name
                changed = True
            if credentials is not _UNSET and credentials is not None:
                if not credentials:
                    raise error("credential_invalid", "credentials cannot be empty")
                row.encrypted_credentials = self._encrypt(credentials)
                row.credential_mask = "****" if row.kind == "search" else mask_credentials(credentials)
                changed = True
            if not changed:
                if row.kind == "search":
                    raise Slice1Error("invalid_field", "no_search_fields_changed", 422)
                return self.public_view(row)
            row.config_version += 1
            row.updated_at = utcnow()
            # 端点/凭据/模型变化后能力状态回到 unknown，需重新探测
            row.capability_status = json.dumps(empty_capability_status(row.protocol))
            row.reasoning_validation = "{}"
            sess.flush()
            self._search_change(sess, row, actor_id, "updated", is_admin=is_admin)
            view = self.public_view(row)
            sess.commit()
        self._notify(config_id, actor_id, "updated")
        return view

    def deactivate(self, config_id: str, *, actor_id: str, is_admin: bool) -> dict:
        with self.db.session() as sess:
            row = self._load(sess, config_id)
            self._authorize(row, actor_id, is_admin=is_admin, write=True)
            row.is_active = False
            row.reasoning_validation = "{}"
            row.updated_at = utcnow()
            row.config_version += 1
            sess.flush()
            self._search_change(sess, row, actor_id, "deactivated", is_admin=is_admin)
            view = self.public_view(row)
            sess.commit()
        self._notify(config_id, actor_id, "deactivated")
        return view

    def set_capability_status(self, config_id: str, status: dict, *, expected_version=None, merge=False, actor_id=None, is_admin=False) -> dict:
        with self.db.session() as sess:
            row = sess.get(ServiceConfigRow, config_id, with_for_update=True)
            if row is None:
                raise error("config_not_found")
            if row.kind == "search" and expected_version is None:
                raise Slice1Error("invalid_field", "search_probe_version_required", 422)
            if expected_version is not None and (not row.is_active or row.config_version != expected_version):
                if row.kind == "search":
                    self._search_audit(sess, config_id, actor_id or "system", "probe_stale", is_admin=is_admin)
                return self.public_view(row)
            if row.kind == "search":
                self._search_audit(sess, config_id, actor_id or "system", "probe_completed", is_admin=is_admin)
            combined = row.status_dict() if merge else {}
            combined.update(status)
            row.capability_status = json.dumps(combined)
            row.updated_at = utcnow()
            sess.flush()
            view = self.public_view(row)
            sess.commit()
        return view

    def active_config_for_worker(self, config_id: str) -> dict:
        """worker 出站前核验：配置必须存在且启用。"""
        with self.db.session() as sess:
            row = sess.get(ServiceConfigRow, config_id)
            if row is None or not row.is_active:
                raise error("config_not_found", "config missing or inactive")
            return self.worker_view(row)


    def _notify(self, config_id: str, actor_id: str, action: str) -> None:
        if self._on_config_changed is not None:
            self._on_config_changed(config_id, actor_id, action)
        refresher = getattr(self,"capability_refresher",None)
        if refresher is not None:
            refresher.changed(config_id,actor_id,action)


def protocol_capabilities(protocol: str) -> tuple[str, ...]:
    return PROTOCOL_CAPABILITIES.get(protocol, ("text",))

"""Immutable, encrypted personal retrieval configurations."""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import Boolean, DateTime, Integer, String, Text, select, text
from sqlalchemy.orm import Mapped, mapped_column

from ..logging_audit.models import Base
from ..slice1.crypto import decrypt, encrypt
from ..slice1.crypto import CredentialCryptoError
from ..slice1.errors import Slice1Error, error
from ..slice1.model_catalog import normalize_base
from ..slice1.reasoning import ReasoningSettings, validate_settings

TARGET_DIMENSIONS = int(os.environ.get("EMBEDDING_TARGET_DIMENSIONS", "1024"))
MEMORY_STORAGE_DIMENSIONS = 2000
MAX_DIMENSIONS = 16000


class PersonalModelRevision(Base):
    __tablename__ = "personal_retrieval_models"
    owner_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    spec_json: Mapped[str] = mapped_column(Text)
    encrypted_keys: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class MemoryBankRoute(Base):
    __tablename__ = "personal_memory_routes"
    bank_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    embedding_space: Mapped[str] = mapped_column(String(64))


class MemoryMigration(Base):
    __tablename__ = "personal_memory_migrations"
    migration_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    space_id: Mapped[str] = mapped_column(String(64))
    source_bank: Mapped[str] = mapped_column(String(64))
    target_bank: Mapped[str] = mapped_column(String(64), unique=True)
    revision: Mapped[int] = mapped_column(Integer)
    epoch: Mapped[int] = mapped_column(Integer)
    source_refs_json: Mapped[str] = mapped_column(Text)
    completed: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    operation_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tag_index: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleanup_done: Mapped[bool] = mapped_column(Boolean, default=False)
    cleanup_attempts: Mapped[int] = mapped_column(Integer, default=0)


class PersonalEmbeddingProfile(Base):
    """Mutable dimension probe for an immutable model revision."""
    __tablename__ = "personal_embedding_profiles"
    owner_user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_space: Mapped[str | None] = mapped_column(String(64), nullable=True)
    memory_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_compatible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class ProviderIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    endpoint: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = Field(default=None, repr=False, max_length=4096)
    reasoning_settings: ReasoningSettings | None = None


class PersonalIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(ge=0)
    content: ProviderIn | None = None
    embedding: ProviderIn
    # Retained for one release so older clients can still save; the value is ignored.
    dimensions: int | None = Field(default=None, exclude=True)
    rerank_protocol: Literal["rerank", "jev"] = "rerank"
    rerank: ProviderIn


class PersonalProbeIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    revision: int = Field(ge=1)
    kind: Literal["content", "embedding", "rerank"]


class PersonalCatalogIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: Literal["content", "embedding", "rerank"]
    protocol: Literal["rerank", "jev"] = "rerank"
    endpoint: str = Field(min_length=1, max_length=500)
    api_key: SecretStr | None = Field(default=None, repr=False, max_length=4096)


class PersonalReasoningIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    endpoint: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = Field(default=None, repr=False, max_length=4096)
    refresh: bool = False


def fingerprint(spec):
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def open_keys(value):
    try:
        return json.loads(decrypt(value))
    except (CredentialCryptoError, ValueError):
        raise error("credential_invalid", "无法读取已保存的密钥，请检查服务器加密设置。") from None


def seal_keys(value):
    try:
        return encrypt(json.dumps(value))
    except CredentialCryptoError:
        raise error("invalid_field", "服务器尚未配置凭据加密，请联系管理员。") from None


def latest(session, owner):
    return session.scalar(select(PersonalModelRevision).where(
        PersonalModelRevision.owner_user_id == owner).order_by(
        PersonalModelRevision.revision.desc()).limit(1))


def space_for(embedding, dimensions):
    return fingerprint({**embedding, "dimensions": dimensions, "encoding_version": 1})


def profile_for(session, owner, row):
    """Get or derive the mutable dimension profile; legacy revisions keep their space."""
    if not row:
        return None
    profile = session.get(PersonalEmbeddingProfile, owner)
    if profile is not None:
        return profile
    spec = json.loads(row.spec_json)
    dimensions = spec.get("dimensions")
    if dimensions:
        profile = PersonalEmbeddingProfile(
            owner_user_id=owner, revision=row.revision, status="detected",
            dimensions=dimensions, request_dimensions=dimensions,
            embedding_space=spec.get("embedding_space") or space_for(spec["embedding"], dimensions),
            memory_dimensions=MEMORY_STORAGE_DIMENSIONS,
            memory_compatible=dimensions <= MEMORY_STORAGE_DIMENSIONS, attempts=0)
    else:
        profile = PersonalEmbeddingProfile(
            owner_user_id=owner, revision=row.revision, status="pending",
            dimensions=TARGET_DIMENSIONS, request_dimensions=TARGET_DIMENSIONS,
            embedding_space=space_for(spec["embedding"], TARGET_DIMENSIONS),
            memory_dimensions=MEMORY_STORAGE_DIMENSIONS,
            memory_compatible=True, attempts=0)
    session.add(profile)
    session.flush()
    return profile


def profile_values(profile):
    if profile is None:
        return {"dimensions": None, "request_dimensions": None, "embedding_space": None,
                "embedding_status": "unconfigured",
                "memory_dimensions": MEMORY_STORAGE_DIMENSIONS,
                "memory_compatible": None}
    return {"dimensions": profile.dimensions, "request_dimensions": profile.request_dimensions,
            "embedding_space": profile.embedding_space, "embedding_status": profile.status,
            "memory_dimensions": profile.memory_dimensions or MEMORY_STORAGE_DIMENSIONS,
            "memory_compatible": profile.memory_compatible}


def public(row):
    if not row:
        return {"revision": 0, "status": "unconfigured",
                "target_dimensions": TARGET_DIMENSIONS,
                "rerank_protocol": "rerank", "content": None,
                "embedding": None, "rerank": None}
    spec = json.loads(row.spec_json)
    for kind in ("content", "embedding", "rerank"):
        if spec.get(kind):
            spec[kind] = {**spec[kind], "credential_mask": "********"}
    return {**spec, "revision": row.revision,
            "status": "active" if row.enabled else "disabled"}


class PersonalModels:
    def __init__(self, db, audit=None):
        self.db, self.audit = db, audit

    def view(self, owner):
        with self.db.session() as session:
            row = latest(session, owner)
            value = public(row)
            profile = profile_for(session, owner, row)
            value.update(profile_values(profile))
            if row:
                routes = list(session.scalars(select(MemoryBankRoute).where(
                    MemoryBankRoute.owner_user_id == owner)))
                space = profile.embedding_space if profile else None
                value["memory_migration_required"] = bool(space) and any(
                    route.embedding_space != space for route in routes)
                from .models import MemorySpaceRow
                spaces = list(session.scalars(select(MemorySpaceRow.bank_id).where(
                    MemorySpaceRow.owner_user_id == owner, MemorySpaceRow.status == "active")))
                value["memory_migration_required"] |= any(
                    bank not in {r.bank_id for r in routes} for bank in spaces)
                jobs = list(session.scalars(select(MemoryMigration).where(
                    MemoryMigration.owner_user_id == owner)))
                value["migrations"] = [{"status": j.status, "completed": j.completed,
                    "total": len(json.loads(j.source_refs_json))} for j in jobs]
            return value

    def _audit(self, owner, result, session=None):
        if self.audit:
            self.audit.append("config.changed", actor_id=owner,
                object_type="personal_retrieval", object_id=owner,
                result=result, session=session)

    def save(self, owner, body):
        from ..auth.models import UserRow
        from sqlalchemy import func
        try:
            with self.db.session() as session:
                user = session.get(UserRow, owner, with_for_update=True)
                if not user or user.status != "approved":
                    raise error("access_denied")
                if session.bind.dialect.name == "postgresql":
                    session.execute(select(func.pg_advisory_xact_lock(
                        func.hashtextextended("personal-models:" + owner, 0))))
                old = latest(session, owner)
                if (old.revision if old else 0) != body.expected_revision:
                    raise Slice1Error("state_invalid", "配置已变化，请重新加载；输入内容已保留。", 409)
                before = json.loads(old.spec_json) if old else {}
                keys = open_keys(old.encrypted_keys) if old else {}
                profile = profile_for(session, owner, old) if old else None
                spec = {"rerank_protocol": body.rerank_protocol}
                for kind in ("content", "embedding", "rerank"):
                    provider = getattr(body, kind)
                    if provider is None:
                        spec[kind] = None
                        keys.pop(kind, None)
                        continue
                    protocol = (body.rerank_protocol if kind == "rerank"
                                else "openai" if kind == "content" else "embedding")
                    base = normalize_base(provider.endpoint, protocol)
                    previous = before.get(kind) or {}
                    key = provider.api_key.get_secret_value().strip() if provider.api_key else ""
                    if not key and (previous.get("endpoint") != base
                            or kind == "rerank" and before.get("rerank_protocol") != body.rerank_protocol):
                        raise error("credential_invalid", "更换地址或协议后，请重新填写 API Key。")
                    key = key or keys.get(kind, "")
                    model = provider.model.strip()
                    if (not key or not key.isprintable() or not model or not model.isprintable()
                            or key == "********"):
                        raise error("invalid_field", "请填写有效的模型和 API Key。")
                    keys[kind] = key
                    if kind == "content":
                        reasoning = validate_settings(provider.reasoning_settings,
                            protocol="openai", endpoint=base, model_name=model)
                        spec[kind] = {"endpoint": base, "model": model,
                                      "reasoning_settings": reasoning}
                    else:
                        if provider.reasoning_settings is not None:
                            raise error("invalid_field", "只有记忆内容模型支持思考强度。")
                        spec[kind] = {"endpoint": base, "model": model}
                row = PersonalModelRevision(owner_user_id=owner,
                    revision=body.expected_revision + 1, spec_json=json.dumps(spec),
                    encrypted_keys=seal_keys(keys), enabled=True)
                session.add(row)
                embedding_changed = before.get("embedding") != spec.get("embedding")
                if profile is None:
                    profile = PersonalEmbeddingProfile(owner_user_id=owner)
                    session.add(profile)
                profile.revision = row.revision
                if embedding_changed or profile.embedding_space is None:
                    # Optimistically use 1024; the first real embedding call verifies it
                    # and falls back to the provider's native dimension when needed.
                    profile.status = "pending"
                    profile.dimensions = TARGET_DIMENSIONS
                    profile.request_dimensions = TARGET_DIMENSIONS
                    profile.embedding_space = space_for(spec["embedding"], TARGET_DIMENSIONS)
                    profile.memory_dimensions = MEMORY_STORAGE_DIMENSIONS
                    profile.memory_compatible = True
                    profile.attempts = 0
                from ..materials.corpus import CorpusIndex
                for index in session.scalars(select(CorpusIndex).where(
                        CorpusIndex.owner_user_id == owner,
                        CorpusIndex.space == profile.embedding_space,
                        CorpusIndex.status.in_(("failed", "blocked", "pending"))).with_for_update()):
                    index.config_revision = row.revision
                    index.status, index.attempts = "pending", 0
                # Same-space key/model-service settings can advance without rewriting vectors.
                # A different embedding space stays bound until an explicit migration publishes.
                for route in session.scalars(select(MemoryBankRoute).where(
                        MemoryBankRoute.owner_user_id == owner).with_for_update()):
                    if route.embedding_space == profile.embedding_space:
                        route.revision = row.revision
                self._audit(owner, "saved", session)
                session.flush()
            return self.view(owner)
        except Slice1Error:
            self._audit(owner, "denied")
            raise

    def resolve(self, owner, revision=None):
        from ..auth.models import UserRow
        with self.db.session() as session:
            user = session.get(UserRow, owner)
            if not user or user.status != "approved":
                raise error("access_denied")
            row = (session.get(PersonalModelRevision, (owner, revision))
                   if revision else latest(session, owner))
            if not row or not row.enabled:
                raise error("config_not_found", "请配置本人的 Embedding 和重排模型。")
            spec = json.loads(row.spec_json)
            profile = profile_for(session, owner, row)
            keys = open_keys(row.encrypted_keys)
            return {**spec, **profile_values(profile), "owner": owner,
                    "revision": row.revision, "keys": keys}

    def check(self, owner, revision):
        self.resolve(owner, revision)

    def ensure_detected(self, owner, revision=None):
        """Verify the optimistic 1024 dimension before the first real embedding."""
        with self.db.session() as session:
            row = (session.get(PersonalModelRevision, (owner, revision))
                   if revision else latest(session, owner))
            if not row or not row.enabled:
                raise error("config_not_found", "请配置本人的 Embedding 和重排模型。")
            profile = profile_for(session, owner, row)
            status, current = profile.status, row.revision
        if status == "pending":
            return self.detect(owner, current)
        return self.resolve(owner, current)

    def detect(self, owner, revision=None, force=False):
        from .providers import ProviderClient
        spec = self.resolve(owner, revision)
        if spec["embedding_status"] == "detected" and not force:
            return spec
        client = ProviderClient(spec, guard=lambda: self.check(owner, spec["revision"]))
        sample = ["synthetic dimension probe"]
        try:
            vectors = client.embed(sample)
            effective = len(vectors[0])
            request_dimensions = TARGET_DIMENSIONS
        except Exception as first:
            if getattr(first, "code", "") in ("access_denied", "disclosure_revoked",
                                              "config_not_found"):
                raise
            try:
                vectors = client.embed(sample, dimensions=None, expected=None)
                effective = len(vectors[0])
                request_dimensions = None
            except Exception as second:
                if getattr(second, "code", "") in ("access_denied", "disclosure_revoked",
                                                   "config_not_found"):
                    raise
                self._mark_profile(owner, spec["revision"], status="failed")
                self._audit(owner, "detect_failed")
                raise error("capability_test_failed",
                    "无法检测 Embedding 维度，请检查模型与密钥。") from None
        if not 32 <= effective <= MAX_DIMENSIONS:
            self._mark_profile(owner, spec["revision"], status="failed")
            raise error("capability_test_failed", "Embedding 维度超出存储范围。")
        if effective != TARGET_DIMENSIONS:
            request_dimensions = None
        storage = MEMORY_STORAGE_DIMENSIONS
        compatible = effective <= storage
        if not compatible:
            try:
                shortened = client.embed(sample, dimensions=storage, expected=storage)
                compatible = len(shortened[0]) == storage
            except Exception:
                compatible = False
        space = space_for(spec["embedding"], effective)
        with self.db.session() as session:
            profile = session.get(PersonalEmbeddingProfile, owner, with_for_update=True)
            old_space = profile.embedding_space if profile else None
            if profile is None:
                profile = PersonalEmbeddingProfile(owner_user_id=owner)
                session.add(profile)
            profile.revision = spec["revision"]
            profile.status = "detected"
            profile.dimensions = effective
            profile.request_dimensions = request_dimensions
            profile.embedding_space = space
            profile.memory_dimensions = storage
            profile.memory_compatible = compatible
            profile.attempts = 0
            if old_space and old_space != space:
                from ..materials.corpus import CorpusIndex
                for index in session.scalars(select(CorpusIndex).where(
                        CorpusIndex.owner_user_id == owner,
                        CorpusIndex.space == old_space).with_for_update()):
                    if index.status in ("ready", "pending"):
                        index.status = "pending"
                        index.lease_until = None
                    index.space, index.attempts = space, 0
                    index.completed, index.vectors_json = 0, "[]"
                if (session.bind.dialect.name == "postgresql"
                        and session.scalar(text("SELECT to_regclass('corpus_vectors') IS NOT NULL"))):
                    session.execute(text(
                        "DELETE FROM corpus_vectors WHERE owner_user_id=:owner AND space=:space"),
                        {"owner": owner, "space": old_space})
            self._audit(owner, "detect_completed", session)
        return self.resolve(owner, spec["revision"])

    def _mark_profile(self, owner, revision, status):
        with self.db.session() as session:
            profile = session.get(PersonalEmbeddingProfile, owner, with_for_update=True)
            if profile is None:
                profile = PersonalEmbeddingProfile(owner_user_id=owner)
                session.add(profile)
            profile.revision = revision
            profile.status = status
            profile.attempts += 1

    def disable(self, owner):
        with self.db.session() as session:
            for row in session.scalars(select(PersonalModelRevision).where(
                    PersonalModelRevision.owner_user_id == owner).with_for_update()):
                row.enabled = False
            self._audit(owner, "disabled", session)
        return self.view(owner)

    def rebuild_memory(self, owner):
        from .models import MemorySettingsRow, MemorySourceRow, MemorySpaceRow
        from .runtime import personal_runtime
        spec = self.resolve(owner)
        if not spec.get("embedding_space"):
            raise error("invalid_field", "请先完成 Embedding 连接测试，再重建记忆。")
        if not spec.get("content") or not personal_runtime()["url"]:
            raise error("invalid_field", "请先配置记忆内容模型，并启动个人记忆服务。")
        with self.db.session() as session:
            setting = session.get(MemorySettingsRow, owner, with_for_update=True)
            if not setting or not setting.enabled:
                raise error("invalid_field", "请先启用个人记忆。")
            for space in session.scalars(select(MemorySpaceRow).where(
                    MemorySpaceRow.owner_user_id == owner, MemorySpaceRow.status == "active")
                    .with_for_update()):
                pending = session.scalar(select(MemoryMigration).where(
                    MemoryMigration.space_id == space.space_id,
                    MemoryMigration.status.in_(("pending", "replaying", "refreshing", "cleanup"))))
                if pending:
                    continue
                if session.scalar(select(MemorySourceRow.source_id).where(
                        MemorySourceRow.owner_user_id == owner,
                        MemorySourceRow.scope_key == space.scope_key,
                        MemorySourceRow.status.in_(("pending", "retained", "forgetting", "forget_failed")))):
                    raise error("invalid_field", "请等待现有记忆处理或遗忘完成后重建。")
                sources = list(session.scalars(select(MemorySourceRow).where(
                    MemorySourceRow.owner_user_id == owner, MemorySourceRow.scope_key == space.scope_key,
                    MemorySourceRow.status.in_(("completed", "compacted"))).order_by(MemorySourceRow.source_id)))
                if not sources:
                    raise error("invalid_field", "没有可安全重放的来源，旧记忆需先导入，不能直接重建。")
                bank = "learning-" + uuid.uuid4().hex
                session.add(MemoryBankRoute(bank_id=bank, owner_user_id=owner,
                    revision=spec["revision"], embedding_space=spec["embedding_space"]))
                session.add(MemoryMigration(migration_id=uuid.uuid4().hex, owner_user_id=owner,
                    space_id=space.space_id, source_bank=space.bank_id, target_bank=bank,
                    revision=spec["revision"], epoch=space.epoch,
                    source_refs_json=json.dumps([[s.source_id, s.revision, s.tag] for s in sources]),
                    completed=0, status="pending"))
            self._audit(owner, "rebuild_requested", session)
        return self.view(owner)

    def catalog(self, owner, body):
        protocol = body.protocol if body.kind == "rerank" else body.kind
        base = normalize_base(body.endpoint, "openai" if protocol == "content" else protocol)
        key = body.api_key.get_secret_value().strip() if body.api_key else ""
        revision = None
        if not key:
            spec = self.resolve(owner)
            saved = spec.get(body.kind) or {}
            if (saved.get("endpoint") != base or body.kind == "rerank"
                    and spec["rerank_protocol"] != body.protocol):
                raise error("credential_invalid", "请为当前地址填写 API Key。")
            key = spec["keys"].get(body.kind, "")
            revision = spec["revision"]
        from .providers import request_json
        self._audit(owner, "probe_started")
        try:
            data = request_json(base, "/v1/models" if protocol == "jev" else "/models",
                                key, None, method="GET",
                                guard=(lambda: self.check(owner, revision)) if revision else None,
                                content_target=body.kind == "content")
            names = {row["id"] for row in data.get("data", [])[:2000]
                     if isinstance(row, dict) and isinstance(row.get("id"), str)
                     and 0 < len(row["id"]) <= 200 and row["id"].isprintable()}
            self._audit(owner, "probe_completed")
            return {"models": sorted(names)[:500]}
        except Exception:
            self._audit(owner, "probe_failed")
            raise error("capability_test_failed", "获取失败，可手动填写模型名。") from None

    async def discover_reasoning(self, owner, body):
        """Probe thinking levels with the typed key or the saved content-model key."""
        from ..slice1.reasoning_discovery import ReasoningDiscovery
        base = normalize_base(body.endpoint, "openai")
        key = body.api_key.get_secret_value().strip() if body.api_key else ""
        revision = None
        if not key:
            try:
                spec = self.resolve(owner)
            except Slice1Error:
                self._audit(owner, "probe_failed")
                raise
            saved = spec.get("content") or {}
            if not saved or saved.get("endpoint") != base:
                self._audit(owner, "probe_failed")
                raise error("credential_invalid", "请为当前地址填写 API Key。")
            key = spec["keys"].get("content", "")
            revision = spec["revision"]
        if not key:
            self._audit(owner, "probe_failed")
            raise error("credential_invalid", "请先填写 API Key。")
        discovery = getattr(self, "_reasoning_discovery", None)
        if discovery is None:
            discovery = ReasoningDiscovery()
            self._reasoning_discovery = discovery
        check = (lambda: self.check(owner, revision)) if revision else (lambda: None)
        return await discovery.detect(actor_id=owner, protocol="openai", endpoint=base,
            model=body.model, key=key, check=check,
            audit=lambda result: self._audit(owner, result), refresh=body.refresh)

    def probe(self, owner, body):
        from .providers import ProviderClient
        spec = self.resolve(owner, body.revision)
        provider = ProviderClient(spec, guard=lambda: self.check(owner, body.revision))
        self._audit(owner, "probe_started")
        try:
            if body.kind == "embedding":
                detected = self.detect(owner, body.revision, force=True)
                detail = {"dimensions": detected["dimensions"],
                          "memory_compatible": detected["memory_compatible"],
                          "requested_dimensions": detected["request_dimensions"]}
            elif body.kind == "rerank":
                provider.rerank("synthetic sample", ["synthetic sample", "unrelated sample"])
                detail = {}
            else:
                provider.chat([{"role": "user", "content": "Reply OK."}], max_tokens=8)
                detail = {}
            self._audit(owner, "probe_completed")
            return {"state": "available", **detail}
        except Exception:
            self._audit(owner, "probe_failed")
            raise error("capability_test_failed", "连接测试未通过，请检查地址、模型和密钥。") from None


def finite_vector(value, dimensions=None):
    if (not isinstance(value, list) or not value
            or dimensions is not None and len(value) != dimensions
            or any(type(x) not in (int, float) or not math.isfinite(x) for x in value)
            or not any(x != 0 for x in value)):
        raise error("capability_test_failed", "Embedding 返回了无效向量。")
    norm = math.sqrt(sum(x * x for x in value))
    if not math.isfinite(norm) or norm <= 0:
        raise error("capability_test_failed", "Embedding 返回了无效向量。")
    return [x / norm for x in value]

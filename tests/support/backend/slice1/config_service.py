"""能力配置 CRUD、凭据加密与掩码（切片 1 T02）。

安全边界：
- 创建时加密凭据，数据库中不出现明文。
- 查询/列表只返回掩码、协议、模型名和能力状态，永不返回完整凭据。
- 普通用户不可读取或修改管理员 OCR/搜索配置。
- 更新凭据或端点生成新 config_version；停用后 worker 拒绝调用。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import select

from ..logging_audit.db import Database
from .crypto import decrypt, encrypt, load_key, mask_credentials
from .errors import Slice1Error, error
from .models import (
    ADMIN_KINDS,
    PROTOCOL_KIND,
    PROTOCOLS,
    PROTOCOL_CAPABILITIES,
    USER_KINDS,
    ServiceConfigRow,
    empty_capability_status,
    utcnow,
)


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
    ) -> None:
        self.db = db
        self._explicit_key = encryption_key
        self._key_cache: Optional[bytes] = None
        # 配置变更回调（T05 用于失效外发授权）
        self._on_config_changed = on_config_changed

    @property
    def _key_material(self) -> bytes:
        # 懒加载：缺少 APP_ENCRYPTION_KEY 时应用仍可启动，只在凭据操作时报受控错误。
        if self._key_cache is None:
            self._key_cache = load_key(self._explicit_key)
        return self._key_cache

    # ---- 内部：加密/解密 ----
    def _encrypt(self, credentials: dict) -> str:
        return encrypt(json.dumps(credentials, ensure_ascii=False, sort_keys=True), self._key_material)

    def _decrypt(self, ciphertext: str) -> dict:
        if not ciphertext:
            return {}
        raw = decrypt(ciphertext, self._key_material)
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
            return self._decrypt(row.encrypted_credentials)

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
            "credential_mask": row.credential_mask,
            "capability_status": row.status_dict(),
            "config_version": row.config_version,
            "is_active": row.is_active,
            "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at),
        }

    # ---- 权限 ----
    @staticmethod
    def _is_admin_config(row: ServiceConfigRow) -> bool:
        return row.owner_scope == "admin"

    def _load(self, sess, config_id: str) -> ServiceConfigRow:
        row = sess.get(ServiceConfigRow, config_id)
        if row is None:
            raise error("config_not_found")
        return row

    def _authorize(self, row: ServiceConfigRow, actor_id: str, *, is_admin: bool, write: bool) -> None:
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
    ) -> dict:
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

        config_id = f"cfg_{uuid.uuid4().hex[:12]}"
        status = empty_capability_status(protocol)
        with self.db.session() as sess:
            row = ServiceConfigRow(
                config_id=config_id,
                owner_user_id=None if owner_scope == "admin" else actor_id,
                owner_scope=owner_scope,
                kind=kind,
                protocol=protocol,
                endpoint=endpoint,
                model_name=model_name,
                encrypted_credentials=self._encrypt(credentials),
                credential_mask=mask_credentials(credentials),
                capability_status=json.dumps(status),
                config_version=1,
                is_active=True,
            )
            sess.add(row)
            sess.flush()
            view = self.public_view(row)
            sess.commit()
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
            stmt = select(ServiceConfigRow).where(ServiceConfigRow.is_active.is_(True))
            if kind:
                stmt = stmt.where(ServiceConfigRow.kind == kind)
            rows = sess.execute(stmt.order_by(ServiceConfigRow.created_at)).scalars().all()
            visible = []
            for row in rows:
                if self._is_admin_config(row):
                    if is_admin:
                        visible.append(self.public_view(row))
                elif row.owner_user_id == actor_id:
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
        credentials: Optional[dict] = None,
    ) -> dict:
        if endpoint is None and model_name is None and credentials is None:
            raise error("invalid_field", "no fields to update")
        with self.db.session() as sess:
            row = self._load(sess, config_id)
            self._authorize(row, actor_id, is_admin=is_admin, write=True)
            changed = False
            if endpoint is not None and endpoint != row.endpoint:
                if not endpoint:
                    raise error("invalid_field", "endpoint cannot be empty")
                row.endpoint = endpoint
                changed = True
            if model_name is not None and model_name != row.model_name:
                row.model_name = model_name
                changed = True
            if credentials is not None:
                if not credentials:
                    raise error("credential_invalid", "credentials cannot be empty")
                row.encrypted_credentials = self._encrypt(credentials)
                row.credential_mask = mask_credentials(credentials)
                changed = True
            if not changed:
                return self.public_view(row)
            row.config_version += 1
            row.updated_at = utcnow()
            # 端点/凭据/模型变化后能力状态回到 unknown，需重新探测
            row.capability_status = json.dumps(empty_capability_status(row.protocol))
            sess.flush()
            view = self.public_view(row)
            sess.commit()
        self._notify(config_id, actor_id, "updated")
        return view

    def deactivate(self, config_id: str, *, actor_id: str, is_admin: bool) -> dict:
        with self.db.session() as sess:
            row = self._load(sess, config_id)
            self._authorize(row, actor_id, is_admin=is_admin, write=True)
            row.is_active = False
            row.updated_at = utcnow()
            row.config_version += 1
            sess.flush()
            view = self.public_view(row)
            sess.commit()
        self._notify(config_id, actor_id, "deactivated")
        return view

    def set_capability_status(self, config_id: str, status: dict) -> dict:
        with self.db.session() as sess:
            row = self._load(sess, config_id)
            row.capability_status = json.dumps(status)
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
            return {
                "config_id": row.config_id,
                "config_version": row.config_version,
                "kind": row.kind,
                "protocol": row.protocol,
                "endpoint": row.endpoint,
                "model_name": row.model_name,
            }

    def _notify(self, config_id: str, actor_id: str, action: str) -> None:
        if self._on_config_changed is not None:
            self._on_config_changed(config_id, actor_id, action)


def protocol_capabilities(protocol: str) -> tuple[str, ...]:
    return PROTOCOL_CAPABILITIES.get(protocol, ("text",))

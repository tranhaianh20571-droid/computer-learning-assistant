"""ADR-022：外部观测内容（Langfuse trace）删除联动。

用户资源删除时 `register_deletion` 登记 pending 记录（不阻塞用户删除）；
`process_pending` 调用平台删除 API 核销，失败置 failed、可重试，全程发
`observability.deletion.scheduled` 状态事件（无内容字段）。

- 平台客户端走 REST（httpx），不引入 Langfuse Python SDK；
- 未配置 LANGFUSE_BASE_URL 时登记照常、核销直接置 failed（reason=unconfigured），
  保证账本完整、上线后再补跑；
- 平台删除端点契约以 Langfuse 公开 API 为准（DELETE /api/public/traces/{id}），
  真实实例联调标记为待执行验证。
"""

from __future__ import annotations

import base64
import os
import uuid
from datetime import datetime, timezone
from typing import List, Optional

import httpx
from sqlalchemy import select

from ..logging_audit.db import Database
from ..logging_audit.logger import StructuredLogger
from .models import ObservabilityDeletionRefRow, utcnow

MAX_ATTEMPTS = 5


class LangfuseTraceDeletionClient:
    """最小 REST 客户端：删除单条 trace。密钥只从环境变量读取。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        public_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        timeout: float = 5.0,
    ) -> None:
        self.base_url = (base_url or os.environ.get("LANGFUSE_BASE_URL") or "").rstrip("/")
        self.public_key = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY") or ""
        self.secret_key = secret_key or os.environ.get("LANGFUSE_SECRET_KEY") or ""
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.public_key and self.secret_key)

    def delete_trace(self, trace_id: str) -> tuple[bool, str]:
        if not self.configured:
            return False, "unconfigured"
        raw = f"{self.public_key}:{self.secret_key}".encode("utf-8")
        headers = {"authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        try:
            resp = httpx.delete(
                f"{self.base_url}/api/public/traces/{trace_id}",
                headers=headers,
                timeout=self.timeout,
            )
        except httpx.HTTPError:
            return False, "network_error"
        if resp.status_code in (200, 204):
            return True, ""
        if resp.status_code == 404:
            # 平台侧已不存在：视为核销成功
            return True, "not_found"
        return False, f"http_{resp.status_code}"


class ObservabilityDeletionService:
    def __init__(
        self,
        db: Database,
        logger: Optional[StructuredLogger] = None,
        client: Optional[LangfuseTraceDeletionClient] = None,
    ) -> None:
        self.db = db
        self.logger = logger
        self.client = client or LangfuseTraceDeletionClient()

    def _log(self, resource_type: str, resource_id: str, status: str, *, reason: str = "") -> None:
        if self.logger is None:
            return
        try:
            self.logger.log(
                "observability.deletion.scheduled",
                level="INFO" if status != "failed" else "WARN",
                resource_type=resource_type,
                resource_id=resource_id,
                platform="langfuse",
                status=status,
                reason=reason or None,
            )
        except Exception:  # noqa: BLE001 — 事件失败不影响删除账本
            pass

    def register_deletion(
        self,
        *,
        owner_user_id: str,
        resource_type: str,
        resource_id: str,
        trace_ids: Optional[List[str]] = None,
    ) -> List[str]:
        """资源删除事务内登记：每条 trace 一行 pending。失败不抛出。"""
        ref_ids: List[str] = []
        rows = []
        for trace_id in trace_ids or [None]:
            ref_id = f"obsdel_{uuid.uuid4().hex[:12]}"
            rows.append(
                ObservabilityDeletionRefRow(
                    ref_id=ref_id,
                    owner_user_id=owner_user_id,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    trace_id=trace_id,
                    status="pending",
                )
            )
            ref_ids.append(ref_id)
        try:
            with self.db.session() as sess:
                sess.add_all(rows)
                sess.commit()
            self._log(resource_type, resource_id, "pending")
        except Exception as exc:  # noqa: BLE001 — 登记失败只记事件，不阻塞用户删除
            self._log(resource_type, resource_id, "failed", reason=type(exc).__name__)
        return ref_ids

    def process_pending(self, limit: int = 50) -> dict:
        """核销 pending 记录；返回 completed/failed/pending 计数。"""
        completed = failed = 0
        with self.db.session() as sess:
            rows = (
                sess.execute(
                    select(ObservabilityDeletionRefRow)
                    .where(ObservabilityDeletionRefRow.status == "pending")
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.attempts += 1
                row.updated_at = utcnow()
                if not row.trace_id:
                    ok, reason = True, "no_trace"
                else:
                    ok, reason = self.client.delete_trace(row.trace_id)
                if ok:
                    row.status = "completed"
                    row.reason = reason
                    completed += 1
                elif row.attempts >= MAX_ATTEMPTS:
                    row.status = "failed"
                    row.reason = reason
                    failed += 1
                else:
                    row.reason = reason
            sess.commit()
        return {"completed": completed, "failed": failed}


__all__ = ["LangfuseTraceDeletionClient", "ObservabilityDeletionService", "MAX_ATTEMPTS"]

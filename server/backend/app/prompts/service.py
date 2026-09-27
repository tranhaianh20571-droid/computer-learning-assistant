"""task_prompt_bindings：固定提示词快照、哈希、local fallback（T05，架构 §3.3）。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import DateTime, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column

from ..logging_audit.db import Database
from ..logging_audit.models import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PromptBindingRow(Base):
    __tablename__ = "task_prompt_bindings"

    binding_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str | None] = mapped_column(String(64), index=True)
    prompt_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)  # langfuse | local_fallback
    langfuse_version: Mapped[str | None] = mapped_column(String(64))
    template_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    template_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_config_version: Mapped[str | None] = mapped_column(String(64))
    output_schema_version: Mapped[str | None] = mapped_column(String(40))
    tools_version: Mapped[str | None] = mapped_column(String(40))
    agent_code_version: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class PromptUnavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.code = "PROMPT_UNAVAILABLE"
        self.reason = reason


class PromptService:
    """本地后备模板目录 + 绑定快照。

    - 任务创建事务内绑定；无绑定不得调用模型
    - 重试/恢复只引用原绑定
    - 哈希不匹配或无后备 → prompt_unavailable
    """

    def __init__(self, db: Database, fallback_dir: Optional[Path] = None) -> None:
        self.db = db
        self.fallback_dir = fallback_dir or Path(__file__).resolve().parent / "fallback_templates"

    def _sha256(self, text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def load_local_template(self, prompt_name: str) -> dict[str, Any]:
        path = self.fallback_dir / f"{prompt_name}.json"
        if not path.exists():
            raise PromptUnavailable(f"no local fallback for {prompt_name}")
        data = json.loads(path.read_text(encoding="utf-8"))
        body = data.get("template", "")
        expected = data.get("sha256")
        actual = self._sha256(body)
        if expected and expected != actual:
            raise PromptUnavailable("fallback hash mismatch")
        return data

    def bind_for_task(
        self,
        task_id: str,
        prompt_name: str,
        *,
        allow_fallback: bool = True,
        source: str = "local_fallback",
        langfuse_version: Optional[str] = None,
        model_config_version: Optional[str] = None,
        output_schema_version: str = "1",
        tools_version: str = "0",
        agent_code_version: str = "0",
    ) -> dict:
        """创建任务时固定绑定；返回 binding_id 与快照哈希。"""
        if source == "langfuse" and not langfuse_version:
            raise PromptUnavailable("langfuse version required")
        if source == "local_fallback":
            if not allow_fallback:
                raise PromptUnavailable("fallback not allowed for this task type")
            data = self.load_local_template(prompt_name)
            snapshot = data["template"]
            sha = self._sha256(snapshot)
        else:
            # Langfuse 路径：生产从 Langfuse 拉取；此处要求调用方传入快照由 adapter 提供
            raise PromptUnavailable("langfuse adapter not configured in this environment")

        binding_id = f"pbind_{uuid.uuid4().hex[:12]}"
        with self.db.session() as sess:
            sess.add(
                PromptBindingRow(
                    binding_id=binding_id,
                    task_id=task_id,
                    prompt_name=prompt_name,
                    source=source,
                    langfuse_version=langfuse_version,
                    template_snapshot=snapshot,
                    template_sha256=sha,
                    model_config_version=model_config_version,
                    output_schema_version=output_schema_version,
                    tools_version=tools_version,
                    agent_code_version=agent_code_version,
                )
            )
            sess.commit()
        return {"binding_id": binding_id, "template_sha256": sha, "source": source}

    def resolve_for_task(self, task_id: str) -> dict:
        """已有任务从快照恢复；无绑定 → prompt_unavailable。"""
        with self.db.session() as sess:
            row = sess.execute(
                select(PromptBindingRow).where(PromptBindingRow.task_id == task_id)
            ).scalars().first()
            if row is None:
                raise PromptUnavailable(f"no binding for task {task_id}")
            if self._sha256(row.template_snapshot) != row.template_sha256:
                raise PromptUnavailable("binding snapshot hash mismatch")
            return {
                "binding_id": row.binding_id,
                "prompt_name": row.prompt_name,
                "source": row.source,
                "template_snapshot": row.template_snapshot,
                "template_sha256": row.template_sha256,
                "langfuse_version": row.langfuse_version,
            }

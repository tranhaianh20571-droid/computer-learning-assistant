"""即时文字对话（T08）。

- 新 user 消息 → conversation.generation += 1 + 任务创建（幂等键）+ 事件；
- 旧 generation 消息留历史不回填；刷新只恢复已保存消息，不自动重发；
- 上下文：资料选择快照 + manifest（不含正文）+ 历史消息范围（预算裁剪）
  + 可选 web 搜索（真实尝试；失败进入对话级 awaiting_search_decision 状态，
  用户重试或明确离线继续后恢复，generation 不重复递增）；
- 输出为纯文字；能力缺失 → MODEL_CAPABILITY_UNAVAILABLE；
- 边界：即时路径不调用板书、TTS 或播放 API（本模块不引用任何相关路径）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from sqlalchemy import select

from ..logging_audit.db import Database
from ..logging_audit.repositories import PgTaskEventStore
from ..prompts.service import PromptService, PromptUnavailable
from ..tasks.service import TaskService
from .errors import PlanningError, WaitStateSignal, error
from .models import (
    ConversationRow,
    MessageRow,
    load_json_field,
)
from .model_runner import run_chat
from .outputs import CONTEXT_BUDGET_DEFAULTS
from .retrieval import PlanningRetrievalService, build_history_range, validate_material_ownership
from .search import SearchGate, SearchOutcome

CHAT_PROMPT_NAME = "chat_answer_v1"
MAX_MESSAGE_LEN = 4000


class ChatService:
    def __init__(
        self,
        db: Database,
        tasks: TaskService,
        events: PgTaskEventStore,
        prompts: PromptService,
        retrieval: PlanningRetrievalService,
        model_adapter_factory: Optional[Any] = None,
        search_adapter_factory: Optional[Any] = None,
    ) -> None:
        self.db = db
        self.tasks = tasks
        self.events = events
        self.prompts = prompts
        self.retrieval = retrieval
        self.model_adapter_factory = model_adapter_factory
        self.search_adapter_factory = search_adapter_factory

    # ------------------------------------------------------------------
    def create_conversation(
        self,
        *,
        owner_user_id: str,
        scenario: str = "self_study",
        material_ids: Optional[list[str]] = None,
        budget: Optional[dict] = None,
    ) -> dict:
        owned = validate_material_ownership(self.db, owner_user_id, material_ids or [])
        conv_id = f"conv_{uuid.uuid4().hex[:12]}"
        with self.db.session() as sess:
            sess.add(
                ConversationRow(
                    conversation_id=conv_id,
                    owner_user_id=owner_user_id,
                    kind="chat",
                    scenario=scenario,
                    generation=0,
                    material_snapshot_json=json.dumps(owned, ensure_ascii=False),
                )
            )
            sess.commit()
        return {
            "conversation_id": conv_id,
            "generation": 0,
            "material_ids": owned,
            "budget": {**CONTEXT_BUDGET_DEFAULTS, **(budget or {})},
        }

    def get_conversation(self, conversation_id: str, *, actor_id: str) -> dict:
        with self.db.session() as sess:
            conv = sess.get(ConversationRow, conversation_id)
            if conv is None or conv.owner_user_id != actor_id or conv.deleted_at is not None:
                raise error("conversation_not_found", status=404)
            messages = (
                sess.execute(
                    select(MessageRow)
                    .where(MessageRow.conversation_id == conversation_id)
                    .order_by(MessageRow.created_at, MessageRow.message_id)
                )
                .scalars()
                .all()
            )
            # 会话级等待态：当前 generation 只有 user 消息且搜索在等待决策
            wait_state = self._conversation_wait_state(conv, messages)
            return {
                "conversation_id": conv.conversation_id,
                "generation": conv.generation,
                "material_ids": load_json_field(conv.material_snapshot_json, []),
                "wait_state": wait_state,
                "messages": [
                    {
                        "message_id": m.message_id,
                        "generation": m.generation,
                        "role": m.role,
                        "content": m.content_ref,
                        "status": m.status,
                    }
                    for m in messages
                ],
            }

    def _conversation_wait_state(self, conv: ConversationRow, messages: list[MessageRow]) -> Optional[str]:
        current = [m for m in messages if m.generation == conv.generation]
        has_user = any(m.role == "user" for m in current)
        has_answer = any(m.role == "assistant" for m in current)
        if has_user and not has_answer:
            return "awaiting_search_decision"
        return None

    # ------------------------------------------------------------------
    def send_message(
        self,
        conversation_id: str,
        *,
        actor_id: str,
        content: str,
        use_search: bool = False,
        idempotency_key: str = "",
    ) -> dict:
        """新追问：任务创建（幂等键先行）→ generation 递增 → 回答。搜索失败 → 等待态。"""
        content = (content or "").strip()
        if not content or len(content) > MAX_MESSAGE_LEN:
            raise error("invalid_field", "message length out of range")
        with self.db.session() as sess:
            conv = sess.get(ConversationRow, conversation_id)
            if conv is None or conv.owner_user_id != actor_id or conv.deleted_at is not None:
                raise error("conversation_not_found", status=404)
            material_ids = load_json_field(conv.material_snapshot_json, [])

        # 幂等键先行：同键重发不递增 generation、不重复落消息
        created = self.tasks.create_task(
            owner_user_id=actor_id,
            kind="chat_answer",
            subject_id=conversation_id,
            idempotency_key=idempotency_key or f"chat:{conversation_id}:{uuid.uuid4().hex[:8]}",
            stage="generate",
        )
        task_id = created["task_id"]
        generation = conv.generation
        if created.get("deduped"):
            return {"task_id": task_id, "generation": generation, "deduped": True}

        with self.db.session() as sess:
            conv = sess.get(ConversationRow, conversation_id)
            conv.generation += 1
            generation = conv.generation
            sess.add(
                MessageRow(
                    message_id=f"msg_{uuid.uuid4().hex[:12]}",
                    conversation_id=conversation_id,
                    generation=generation,
                    role="user",
                    content_ref=content,
                    status="completed",
                )
            )
            sess.flush()

        try:
            binding = self.prompts.bind_for_task(task_id, CHAT_PROMPT_NAME, allow_fallback=True)
        except PromptUnavailable as exc:
            raise error("PROMPT_UNAVAILABLE", exc.reason, status=409)

        return self._answer(
            conversation_id, generation, task_id, actor_id,
            content=content, material_ids=material_ids,
            use_search=use_search, search_decision=None, binding_id=binding["binding_id"],
        )

    # ------------------------------------------------------------------
    # 流式回答（切片 4 补充）：delta 为易失事件；完成才落库；断存 interrupted
    # ------------------------------------------------------------------
    def _begin_turn(self, conversation_id: str, *, actor_id: str, content: str,
                    idempotency_key: str) -> dict:
        """与 send_message 相同的准备语义：校验 → 幂等任务 → generation/用户消息。"""
        content = (content or "").strip()
        if not content or len(content) > MAX_MESSAGE_LEN:
            raise error("invalid_field", "message length out of range")
        with self.db.session() as sess:
            conv = sess.get(ConversationRow, conversation_id)
            if conv is None or conv.owner_user_id != actor_id or conv.deleted_at is not None:
                raise error("conversation_not_found", status=404)
            material_ids = load_json_field(conv.material_snapshot_json, [])
        created = self.tasks.create_task(
            owner_user_id=actor_id,
            kind="chat_answer",
            subject_id=conversation_id,
            idempotency_key=idempotency_key or f"chat:{conversation_id}:{uuid.uuid4().hex[:8]}",
            stage="generate",
        )
        task_id = created["task_id"]
        if created.get("deduped"):
            with self.db.session() as sess:
                conv = sess.get(ConversationRow, conversation_id)
                generation = conv.generation
            return {"conversation_id": conversation_id, "generation": generation,
                    "task_id": task_id, "material_ids": material_ids, "deduped": True}
        with self.db.session() as sess:
            conv = sess.get(ConversationRow, conversation_id)
            conv.generation += 1
            generation = conv.generation
            sess.add(
                MessageRow(
                    message_id=f"msg_{uuid.uuid4().hex[:12]}",
                    conversation_id=conversation_id,
                    generation=generation,
                    role="user",
                    content_ref=content,
                    status="completed",
                )
            )
            sess.flush()
        return {"conversation_id": conversation_id, "generation": generation,
                "task_id": task_id, "material_ids": material_ids, "deduped": False}

    def stream_answer(self, conversation_id: str, *, actor_id: str, content: str,
                      use_search: bool, idempotency_key: str):
        """流式版即时回答：完成准备（4xx 在此处抛出），返回 (meta, 事件生成器)。

        事件（SSE data JSON）：delta{text} / done{message_id, content} /
        error{error_code} / wait{wait_state}。delta 不落库；done 时完整
        回答落库；客户端断开时已产出文本以 interrupted 状态保留。
        """
        turn = self._begin_turn(conversation_id, actor_id=actor_id,
                                content=content, idempotency_key=idempotency_key)
        meta = {"task_id": turn["task_id"], "generation": turn["generation"],
                "conversation_id": conversation_id, "deduped": turn["deduped"]}

        def gen():
            accumulated = ""
            try:
                if turn["deduped"]:
                    yield {"type": "error", "error_code": "DUPLICATE_REQUEST"}
                    return
                try:
                    binding = self.prompts.bind_for_task(turn["task_id"], CHAT_PROMPT_NAME,
                                                         allow_fallback=True)
                except PromptUnavailable as exc:
                    yield {"type": "error", "error_code": "PROMPT_UNAVAILABLE"}
                    self.events.record(turn["task_id"], stage="generate", status="failed",
                                       error_code="PROMPT_UNAVAILABLE")
                    return
                retrieval = self.retrieval.assemble(
                    owner_user_id=actor_id, material_ids=turn["material_ids"], query=content)
                history_rows = self._history(conversation_id, actor_id, turn["generation"])
                kept, history_range = build_history_range(
                    history_rows, CONTEXT_BUDGET_DEFAULTS["max_history_messages"])
                external_refs: list[dict] = []
                if use_search:
                    try:
                        outcome = self._search(actor_id, turn["task_id"], content, None)
                        if outcome is not None:
                            external_refs = outcome.external_refs
                    except WaitStateSignal:
                        yield {"type": "wait", "wait_state": "awaiting_search_decision"}
                        return
                manifest = self.retrieval.save_manifest(
                    task_id=turn["task_id"], retrieval=retrieval,
                    history_range=history_range, extra_refs=external_refs)
                _ = (kept, manifest, binding)

                adapter = (self.model_adapter_factory(actor_id, turn["task_id"])
                           if self.model_adapter_factory else None)
                if adapter is None:
                    self.events.record(turn["task_id"], stage="generate", status="failed",
                                       error_code="MODEL_CAPABILITY_UNAVAILABLE")
                    yield {"type": "error", "error_code": "MODEL_CAPABILITY_UNAVAILABLE"}
                    return
                messages = [
                    {"role": "system", "content": "你是学习助手，用中文简洁回答。"},
                    {"role": "user", "content": content},
                ]
                if hasattr(adapter, "stream"):
                    for delta in adapter.stream(messages, plain_text=True):
                        accumulated += str(delta)
                        yield {"type": "delta", "text": str(delta)}
                else:
                    answer = run_chat(adapter, messages, max_len=MAX_MESSAGE_LEN)
                    accumulated = answer
                    yield {"type": "delta", "text": answer}
                if not accumulated.strip():
                    self.events.record(turn["task_id"], stage="generate", status="failed",
                                       error_code="MODEL_CAPABILITY_UNAVAILABLE")
                    yield {"type": "error", "error_code": "MODEL_CAPABILITY_UNAVAILABLE"}
                    return
                msg = self._record_answer(conversation_id, turn["generation"],
                                          status="completed", content=accumulated)
                self.events.record(turn["task_id"], stage="generate", status="succeeded")
                yield {"type": "done", "message_id": msg["message_id"],
                       "content": accumulated,
                       "external_result_count": len(external_refs)}
            except WaitStateSignal:
                yield {"type": "wait", "wait_state": "awaiting_search_decision"}
            except PlanningError as exc:
                self.events.record(turn["task_id"], stage="generate", status="failed",
                                   error_code=exc.code)
                yield {"type": "error", "error_code": exc.code}
            except GeneratorExit:
                # 客户端断开：已产出文本以 interrupted 状态保留（留历史、不覆盖）
                if accumulated.strip():
                    self._record_answer(conversation_id, turn["generation"],
                                        status="interrupted", content=accumulated)
                raise

        return meta, gen()

    def resume_message(
        self,
        conversation_id: str,
        *,
        actor_id: str,
        decision: str,
    ) -> dict:
        """搜索等待后的恢复：重试搜索或离线继续；generation 不再递增。"""
        if decision not in ("retry", "offline_continue"):
            raise error("invalid_field", "decision must be retry | offline_continue")
        with self.db.session() as sess:
            conv = sess.get(ConversationRow, conversation_id)
            if conv is None or conv.owner_user_id != actor_id:
                raise error("conversation_not_found", status=404)
            generation = conv.generation
            row = (
                sess.execute(
                    select(MessageRow).where(
                        MessageRow.conversation_id == conversation_id,
                        MessageRow.generation == generation,
                        MessageRow.role == "user",
                    )
                    .order_by(MessageRow.created_at.desc())
                )
                .scalars()
                .first()
            )
            if row is None:
                raise error("state_invalid", "no pending question", status=409)
            content = row.content_ref
            material_ids = load_json_field(conv.material_snapshot_json, [])

        created = self.tasks.create_task(
            owner_user_id=actor_id,
            kind="chat_answer",
            subject_id=conversation_id,
            idempotency_key=f"chat-resume:{conversation_id}:{generation}:{decision}",
            stage="generate",
        )
        task_id = created["task_id"]
        try:
            binding = self.prompts.bind_for_task(task_id, CHAT_PROMPT_NAME, allow_fallback=True)
            binding_id: Optional[str] = binding["binding_id"]
        except PromptUnavailable:
            binding_id = None
        return self._answer(
            conversation_id, generation, task_id, actor_id,
            content=content, material_ids=material_ids,
            use_search=True, search_decision=decision, binding_id=binding_id,
        )

    # ------------------------------------------------------------------
    def _answer(
        self,
        conversation_id: str,
        generation: int,
        task_id: str,
        actor_id: str,
        *,
        content: str,
        material_ids: list[str],
        use_search: bool,
        search_decision: Optional[str],
        binding_id: Optional[str],
    ) -> dict:
        # 上下文组装：manifest（不含正文）+ 历史范围
        retrieval = self.retrieval.assemble(
            owner_user_id=actor_id, material_ids=material_ids, query=content
        )
        history_rows = self._history(conversation_id, actor_id, generation)
        kept, history_range = build_history_range(
            history_rows, CONTEXT_BUDGET_DEFAULTS["max_history_messages"]
        )
        external_refs: list[dict] = []
        if use_search:
            outcome = self._search(actor_id, task_id, content, search_decision)
            if outcome is not None:
                external_refs = outcome.external_refs
        manifest = self.retrieval.save_manifest(
            task_id=task_id,
            retrieval=retrieval,
            history_range=history_range,
            extra_refs=external_refs,
        )

        adapter = self.model_adapter_factory(actor_id, task_id) if self.model_adapter_factory else None
        if adapter is None:
            self.events.record(task_id, stage="generate", status="failed",
                               error_code="MODEL_CAPABILITY_UNAVAILABLE")
            return {"task_id": task_id, "generation": generation, "message": None,
                    "error_code": "MODEL_CAPABILITY_UNAVAILABLE",
                    "manifest_id": manifest["manifest_id"]}
        messages = [
            {"role": "system", "content": "你是学习助手，用中文简洁回答。"},
            {"role": "user", "content": content},
        ]
        # S1b：经 AgentRunner（同步形态冻结，见 agents/runner.py docstring / R3）。
        # 流式路径（stream_answer 的 adapter.stream）保留直连：delta 等价零风险
        # 优先于"全走 Agent"，偏差记入方案 §9。
        from ..agents.runner import run_chat_agent

        answer = run_chat_agent(adapter, messages, max_len=MAX_MESSAGE_LEN)
        answer_message = self._record_answer(conversation_id, generation, status="completed", content=answer)
        self.events.record(task_id, stage="generate", status="succeeded")
        return {
            "task_id": task_id,
            "generation": generation,
            "message": answer_message,
            "manifest_id": manifest["manifest_id"],
            "prompt_binding_id": binding_id,
            "external_result_count": len(external_refs),
        }

    def _search(self, actor_id: str, task_id: str, query: str, decision: Optional[str]) -> Optional[SearchOutcome]:
        adapter = self.search_adapter_factory(actor_id, task_id) if self.search_adapter_factory else None
        gate = SearchGate(adapter)
        outcome = gate.attempt(query[:200])
        gate.require_success_or_wait(outcome, decision=decision)
        return outcome if outcome.ok else None

    def _history(self, conversation_id: str, actor_id: str, generation: int) -> list[dict]:
        with self.db.session() as sess:
            conv = sess.get(ConversationRow, conversation_id)
            if conv is None or conv.owner_user_id != actor_id:
                return []
            rows = (
                sess.execute(
                    select(MessageRow)
                    .where(
                        MessageRow.conversation_id == conversation_id,
                        MessageRow.generation < generation,
                    )
                    .order_by(MessageRow.created_at, MessageRow.message_id)
                )
                .scalars()
                .all()
            )
            return [{"role": r.role, "content": r.content_ref} for r in rows]

    def _record_answer(self, conversation_id: str, generation: int, *, status: str, content: str) -> dict:
        with self.db.session() as sess:
            row = MessageRow(
                message_id=f"msg_{uuid.uuid4().hex[:12]}",
                conversation_id=conversation_id,
                generation=generation,
                role="assistant",
                content_ref=content,
                status=status,
            )
            sess.add(row)
            sess.flush()
            view = {
                "message_id": row.message_id,
                "generation": generation,
                "role": "assistant",
                "content": content,
                "status": status,
            }
            sess.commit()
            return view


__all__ = ["ChatService", "CHAT_PROMPT_NAME"]

"""ADR-028 orchestration: durable turns, fenced workers and private cover assets."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextvars import copy_context
from datetime import datetime, timezone
from hashlib import sha256
import json
import threading
import uuid
from sqlalchemy import select
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelRequest, ModelResponse, UserPromptPart, TextPart
from ..agents.contracts import DialogueInput, RevisionInput, CoverInput
from ..agents.roles import dialogue_agent, revision_agent, cover_agent
from ..agents.runner import deps_for_source
from ..tasks.service import LearningTaskRow
from .adjustments import DialogueAnswer, RevisionAnswer, apply_operations, differences, ensure_refs, node_catalog
from .feature_models import PlanningDialogueRow, CoverAssetRow, CoverStateRow
from .ink import InkRecipe, CoverAnswer, render_ink
from .models import PlanningDraftRow, PlanningConfirmationRow, PlanningTaskStateRow, LearningRouteRow, CourseRow, load_json_field, utcnow
from .errors import error
from .scene import draft_scene


FEATURE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="planning-features")


class PlanningFeatureService:
    def __init__(self, planning, drafts, *, image_adapter_factory=None):
        self.planning, self.drafts = planning, drafts
        self.db, self.tasks, self.prompts = planning.db, planning.tasks, planning.prompts
        self.image_adapter_factory = image_adapter_factory
        self.pool = FEATURE_POOL
        self.active = set()
        self.lock = threading.Lock()
        self.automatic_cover_lock = threading.RLock()
        # ADR-034: production enables configured covers (slice3_app); injected legacy callers keep
        # explicit consent, and a user without an enabled image model keeps the local fallback.
        self.cover_automatic_authorization = False
        from .cover_background import CoverBackground
        self.cover_background = CoverBackground(self)
        planning.cover_service = self

    def target(self, sess, kind, ident, owner):
        model = {"route": LearningRouteRow, "course": CourseRow}.get(kind)
        row = sess.get(model, ident) if model else None
        if row is None or row.owner_user_id != owner or row.deleted_at:
            raise error("draft_not_found", status=404)
        from .locking import planning_target
        planning_target(sess, kind, ident, owner)
        return row

    def draft(self, sess, ident, owner, lock=False):
        row = sess.get(PlanningDraftRow, ident, with_for_update=lock)
        if row is None or row.owner_user_id != owner:
            raise error("draft_not_found", status=404)
        self.target(sess, "route" if row.draft_kind == "route" else "course", row.target_id, owner)
        if lock:
            from .locking import planning_target
            planning_target(sess, row.draft_kind, row.target_id, owner, mutable=True)
        return row

    def _bind(self, task_id, names):
        from ..prompts.service import PromptBindingRow
        for name in names:
            with self.db.session() as sess:
                exists = sess.execute(select(PromptBindingRow.binding_id).where(
                    PromptBindingRow.task_id == task_id, PromptBindingRow.prompt_name == name)).first()
            if exists:
                self.prompts.resolve_for_task(task_id, name)
            else:
                self.prompts.bind_for_task(task_id, name, tools_version="2", agent_code_version="2")

    def enqueue_dialogue(self, draft_id, *, owner, revision, content, key, selected_node=""):
        with self.db.session() as sess:
            draft = self.draft(sess, draft_id, owner, lock=True)
            if draft.revision != revision:
                raise error("DRAFT_REVISION_MISMATCH", status=409)
            confirmed = sess.execute(select(PlanningConfirmationRow).where(
                PlanningConfirmationRow.draft_id == draft_id, PlanningConfirmationRow.revision == revision)).scalars().first()
            if confirmed: raise error("state_invalid", "请先建立编辑草稿", status=409)
            baseline = ensure_refs(load_json_field(draft.content_json, {}), draft.draft_kind)
            catalog = node_catalog(baseline, draft.draft_kind)
            if selected_node:
                selected_node = next((n["node_ref"] for n in catalog if selected_node in (n["path"], n["node_ref"])), "")
            draft.content_json = json.dumps(baseline, ensure_ascii=False)
            idem = "dialogue:" + sha256(f"{draft_id}:{key}".encode()).hexdigest()
            task = self.tasks.create_task(owner_user_id=owner, kind="planning_dialogue", subject_id=draft.target_id,
                                          idempotency_key=idem, stage="generate")
            existing = sess.get(PlanningDialogueRow, task["task_id"])
            if existing is None:
                sess.add(PlanningDialogueRow(task_id=task["task_id"], draft_id=draft_id, owner_user_id=owner,
                    base_revision=revision, user_content=content, selected_node=selected_node,
                    base_json=json.dumps(baseline, ensure_ascii=False)))
            sess.commit()
        self._bind(task["task_id"], ["planning_dialogue_v1", "planning_revise_v1"])
        self.prepare_dialogue(task["task_id"], owner)
        self.kick(task["task_id"])
        return {"task_id": task["task_id"], "draft": self.drafts.get_draft(draft_id, actor_id=owner)}

    def prepare_dialogue(self, task_id, owner):
        if self.planning.authorization is None:
            return
        from .authorization import dialogue_state
        from .locking import planning_target
        with self.db.session() as session:
            state = dialogue_state(session, task_id, owner)
            planning_target(session, state.target_kind, state.target_id, owner, mutable=True)
            task = session.get(LearningTaskRow, task_id, with_for_update=True)
            self.planning.authorization.prepare(session, task, state, owner, allow_refresh=True)

    def _latest_turn(self, sess, draft_id):
        return sess.execute(select(PlanningDialogueRow).where(PlanningDialogueRow.draft_id == draft_id)
            .order_by(PlanningDialogueRow.created_at.desc(), PlanningDialogueRow.task_id.desc())).scalars().first()

    def _recoverable(self, task):
        return bool(task and (task.status in ("queued", "expired") or
            task.status in ("leased", "running") and task.lease_until and task.lease_until <= utcnow()))

    def dialogue(self, draft_id, owner):
        with self.db.session() as sess:
            draft = self.draft(sess, draft_id, owner)
            rows = sess.execute(select(PlanningDialogueRow, LearningTaskRow).outerjoin(
                LearningTaskRow, LearningTaskRow.task_id == PlanningDialogueRow.task_id)
                .where(PlanningDialogueRow.draft_id == draft_id)
                .order_by(PlanningDialogueRow.created_at, PlanningDialogueRow.task_id)).all()
            result, pending = [], []
            latest = rows[-1][0].task_id if rows else None
            for row, task in rows:
                output = load_json_field(row.result_json, {})
                item = {"task_id": row.task_id, "content": row.user_content, "base_revision": row.base_revision,
                    "status": task.status if task else "failed", "decision": row.decision,
                    "result": output, "current": row.task_id == latest,
                    "stale": row.base_revision != draft.revision or row.task_id != latest}
                if output.get("candidate"):
                    item["scene"] = draft_scene(draft.draft_kind, output["candidate"])
                result.append(item)
                if self._recoverable(task):
                    pending.append(row.task_id)
            view = self.drafts._view(draft)
        for task_id in pending: self.kick(task_id)
        return {"turns": result, "draft": view}

    def decide(self, task_id, owner, revision, adopt):
        with self.db.session() as sess:
            row = sess.get(PlanningDialogueRow, task_id)
            if row is None or row.owner_user_id != owner: raise error("task_not_found", status=404)
            draft = self.draft(sess, row.draft_id, owner, lock=True)
            # Lock order is draft -> task everywhere publishing can race with a decision.
            sess.refresh(row, with_for_update=True)
            output = load_json_field(row.result_json, {})
            if not adopt:
                row.decision = "discarded"; sess.commit()
                return {"discarded": True}
            if row.decision == "adopted": return {"draft": self.drafts._view(draft), "deduped": True}
            latest = self._latest_turn(sess, row.draft_id)
            if (row.decision != "pending" or draft.revision != revision or revision != row.base_revision
                or latest.task_id != task_id): raise error("DRAFT_REVISION_MISMATCH", status=409)
            confirmed = sess.execute(select(PlanningConfirmationRow).where(
                PlanningConfirmationRow.draft_id == draft.draft_id, PlanningConfirmationRow.revision == revision)).scalars().first()
            if confirmed: raise error("DRAFT_REVISION_MISMATCH", status=409)
            task = sess.get(LearningTaskRow, task_id)
            if not output.get("candidate") or not task or task.status != "succeeded": raise error("state_invalid", status=409)
            from .outputs import validate_content_json
            validate_content_json(draft.draft_kind, output["candidate"])
            draft.content_json = json.dumps(output["candidate"], ensure_ascii=False)
            draft.revision += 1; draft.updated_at = utcnow(); row.decision = "adopted"
            view = self.drafts._view(draft); sess.commit()
            return {"draft": view}

    def enqueue_automatic_cover(self, kind, ident, owner, **options):
        from .automatic_cover import enqueue
        with self.automatic_cover_lock:
            return enqueue(self, kind, ident, owner, **options)

    def cover_preflight(self, kind, ident, owner):
        from .automatic_cover import preflight
        return preflight(self, kind, ident, owner)

    def enqueue_cover(self, kind, ident, owner, key, method="procedural", automatic=False, regenerate=False):
        if automatic and method == "procedural" and self.cover_automatic_authorization:
            from .automatic_cover import enqueue
            with self.automatic_cover_lock:
                automatic_view = enqueue(self, kind, ident, owner, authorized=False, automatic=True)
            if automatic_view is not None:
                return automatic_view
        if method != "procedural":
            raise error("disclosure_required", "图片封面请先确认主题与模型")
        from .automatic_cover import topic_hash
        from .cover_policy import resolve, suppressed
        with self.db.session() as sess:
            target = self.target(sess, kind, ident, owner)
            sess.refresh(target, with_for_update=True)
            policy = resolve(sess, kind, ident, owner)
            if not policy["cover_eligible"]:
                return suppressed(policy)
            if not regenerate:
                reusable = self._reusable_cover(sess, kind, ident, owner, target.title_ref)
                if reusable:
                    return reusable
            digest = topic_hash(target.title_ref)
            identity = json.dumps([owner, kind, ident, digest, method, key if regenerate else "default"])
            task = self.tasks.create_task(owner_user_id=owner, kind="course_cover", subject_id=ident,
                idempotency_key="cover:"+sha256(identity.encode()).hexdigest(), stage="generate",
                deadline_seconds=300, session=sess)
            asset = sess.scalar(select(CoverAssetRow).where(CoverAssetRow.task_id == task["task_id"]))
            state = sess.get(CoverStateRow, (kind, ident))
            if asset is None:
                asset = CoverAssetRow(asset_id=f"cover_{uuid.uuid4().hex[:16]}", task_id=task["task_id"],
                    target_kind=kind, target_id=ident, owner_user_id=owner, method=method,
                    recipe_json=json.dumps({"title": target.title_ref, "title_hash": digest,
                        "requested_method": method, "actual_method": None, "fallback_reason": None, "error_code": None}, ensure_ascii=False))
                sess.add(asset)
                if state is None:
                    state = CoverStateRow(target_kind=kind, target_id=ident, owner_user_id=owner)
                    sess.add(state)
                state.latest_task_id = task["task_id"]
                sess.flush()
            result = self._cover_view(sess, state)
            sess.commit()
        self.kick(task["task_id"])
        return result

    def _reusable_cover(self, sess, kind, ident, owner, title):
        from .automatic_cover import topic_hash, _config
        state = sess.get(CoverStateRow, (kind, ident))
        if not state or state.owner_user_id != owner:
            return None
        pending = sess.scalar(select(CoverAssetRow).where(CoverAssetRow.task_id == state.latest_task_id)) if state.latest_task_id else None
        task = sess.get(LearningTaskRow, state.latest_task_id) if state.latest_task_id else None
        def matches(asset):
            metadata = load_json_field(asset.recipe_json, {}) if asset else {}
            return bool(asset and asset.owner_user_id == owner and asset.target_kind == kind and asset.target_id == ident
                and metadata.get("title") == title and metadata.get("title_hash") == topic_hash(title))
        if (matches(pending) and task and task.owner_user_id == owner and not task.cancel_requested
                and task.status in ("queued", "leased", "running")
                and (task.deadline_at is None or task.deadline_at > utcnow())):
            metadata = load_json_field(pending.recipe_json, {})
            if metadata.get("automatic"):
                config = _config(sess, owner, metadata.get("config_id"), metadata.get("config_version"))
                from ..slice1.errors import Slice1Error
                from ..logging_audit.contracts import normalize_error_code
                try:
                    self.cover_disclosures.verify_or_audit(task_id=pending.task_id, config_id=config.config_id,
                        actor_id=owner, content_category="generated_text", scope_snapshot=metadata["scope_snapshot"],
                        expected_grant_id=metadata["grant_id"],
                        expected_root_grant_id=metadata.get("parent_grant_id") or metadata["grant_id"], session=sess)
                except Slice1Error as exc:
                    raise error(normalize_error_code(exc.code), status=exc.status) from None
            return self._cover_view(sess, state)
        current = sess.get(CoverAssetRow, state.current_asset_id) if state.current_asset_id else None
        source = sess.get(LearningTaskRow, current.task_id) if current else None
        if matches(current) and current.image_bytes and source and source.owner_user_id == owner and source.status == "succeeded":
            return self._cover_view(sess, state)
        return None

    def reuse_cover(self, source_kind, source_id, kind, ident, owner, *, session=None):
        from .automatic_cover import reuse
        return reuse(self, source_kind, source_id, kind, ident, owner, session=session)

    def _cover_view(self, sess, state):
        from ..logging_audit.models import TaskStateRow
        from ..logging_audit.contracts import ERROR_CODES, normalize_error_code
        from .cover_policy import resolve, suppressed
        policy = resolve(sess, state.target_kind, state.target_id, state.owner_user_id)
        if not policy["cover_eligible"]:
            return suppressed(policy)
        task = sess.get(LearningTaskRow, state.latest_task_id) if state.latest_task_id else None
        asset = sess.get(CoverAssetRow, state.current_asset_id) if state.current_asset_id else None
        target = self.target(sess, state.target_kind, state.target_id, state.owner_user_id)
        if asset:
            metadata = load_json_field(asset.recipe_json, {})
            source = sess.get(LearningTaskRow, asset.task_id)
            if (asset.owner_user_id != state.owner_user_id or asset.target_kind != state.target_kind
                    or asset.target_id != state.target_id or not asset.image_bytes
                    or metadata.get("title") != target.title_ref or not source or source.status != "succeeded"):
                asset = None
        pending = sess.scalar(select(CoverAssetRow).where(CoverAssetRow.task_id == state.latest_task_id)) if state.latest_task_id else None
        metadata = load_json_field(pending.recipe_json, {}) if pending else {}
        task_state = sess.get(TaskStateRow, state.latest_task_id) if state.latest_task_id else None
        code = metadata.get("error_code") or (task_state.error_code if task_state else None)
        code = normalize_error_code(code) if code else None
        return {**policy, "asset_id": asset.asset_id if asset else None, "task_id": state.latest_task_id,
            "status": task.status if task else "not_generated", "method": asset.method if asset else None,
            "requested_method": metadata.get("requested_method", pending.method if pending else None),
            "actual_method": metadata.get("actual_method"), "fallback_reason": metadata.get("fallback_reason"),
            "error_code": code, "retryable": bool(code and ERROR_CODES[code].retryable),
            "deadline_at": task.deadline_at.isoformat() if task and task.deadline_at else None,
            "image_url": f"/api/planning/covers/{asset.asset_id}/image" if asset else None}

    def cover(self, kind, ident, owner):
        with self.db.session() as sess:
            self.target(sess, kind, ident, owner)
            from .cover_policy import resolve, suppressed
            policy = resolve(sess, kind, ident, owner)
            if not policy["cover_eligible"]:
                return suppressed(policy)
            state = sess.get(CoverStateRow, (kind, ident))
            if state is None:
                return {**policy, "status": "not_generated", "asset_id": None, "task_id": None, "image_url": None, "requested_method": None,
                        "actual_method": None, "fallback_reason": None, "error_code": None, "retryable": False}
            return self._cover_view(sess, state)

    def image(self, asset_id, owner):
        with self.db.session() as sess:
            asset = sess.get(CoverAssetRow, asset_id)
            if asset is None or asset.owner_user_id != owner: raise error("draft_not_found", status=404)
            target = self.target(sess, asset.target_kind, asset.target_id, owner)
            from .cover_policy import resolve
            if not resolve(sess, asset.target_kind, asset.target_id, owner)["cover_eligible"]:
                raise error("draft_not_found", status=404)
            if load_json_field(asset.recipe_json, {}).get("title") != target.title_ref:
                raise error("draft_not_found", status=404)
            source = sess.get(LearningTaskRow, asset.task_id)
            if not asset.image_bytes or not source or source.owner_user_id != owner or source.status != "succeeded":
                raise error("draft_not_found", status=404)
            return asset.image_bytes

    def kick(self, task_id):
        if self.tasks.get_task(task_id)["kind"] == "course_cover":
            self.cover_background.wake.set()
            return
        with self.lock:
            if task_id in self.active: return
            self.active.add(task_id)
        self.pool.submit(copy_context().run, self._run, task_id)

    def _run(self, task_id):
        lease = None
        try:
            task = self.tasks.get_task(task_id)
            if task["kind"] != "course_cover" and task["status"] == "expired":
                self.tasks.requeue_for_retry(task_id, actor_id=task["owner_user_id"])
            lease = self.tasks.claim(task_id, lease_owner="planning-feature-worker", lease_seconds=120,
                allow_reclaim_active=False)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(copy_context().run, self._compute, task_id, lease)
                while True:
                    try: output = future.result(timeout=30); break
                    except FutureTimeout: self.tasks.heartbeat(lease, lease_seconds=120)
            def publish(sess):
                turn = sess.get(PlanningDialogueRow, task_id)
                if turn:
                    self.draft(sess, turn.draft_id, turn.owner_user_id, lock=True)
                    turn.result_json = json.dumps(output["result"], ensure_ascii=False)
                    turn.messages_json = output.get("messages_json", "[]")
                else:
                    asset = sess.execute(select(CoverAssetRow).where(CoverAssetRow.task_id == task_id)).scalar_one()
                    parent = self.target(sess, asset.target_kind, asset.target_id, asset.owner_user_id)
                    sess.refresh(parent, with_for_update=True)
                    from .cover_policy import require_eligible
                    require_eligible(sess, asset.target_kind, asset.target_id, asset.owner_user_id)
                    metadata = load_json_field(asset.recipe_json, {})
                    if metadata.get("automatic"):
                        from .automatic_cover import validate
                        validate(self, asset, metadata, lease, session=sess)
                    elif metadata.get("title") is not None and metadata["title"] != parent.title_ref:
                        raise error("OUTPUT_INVALID", "知识主题已变更")
                    asset.image_bytes = output["image"]
                    asset.recipe_json = json.dumps(output["recipe"], ensure_ascii=False)
                    state = sess.get(CoverStateRow, (asset.target_kind, asset.target_id), with_for_update=True)
                    if not state or state.latest_task_id != task_id:
                        raise error("LEASE_LOST", "封面请求已被替换", status=409)
                    if output["recipe"].get("method") == "procedural": asset.method = "procedural"
                    keep_previous = output["recipe"].get("fallback") and state.current_asset_id
                    if state.latest_task_id == task_id and not keep_previous: state.current_asset_id = asset.asset_id
            result = self.tasks.submit_result(lease, stage="generate", status="succeeded", duration_ms=0, publish=publish)
            if not result.get("accepted"):
                self.tasks.recover_worker_failure(lease, stage="generate", error_code="LEASE_LOST")
        except Exception as exc:
            from ..logging_audit.contracts import normalize_error_code
            code = normalize_error_code(getattr(exc, "code", None))
            if lease:
                if code == "TASK_CANCELLED":
                    self.tasks.request_cancel(task_id, actor_id=lease.owner_user_id)
                    self.tasks.recover_worker_failure(lease, stage="generate", error_code=code)
                    return
                def publish_failure(sess):
                    asset = sess.scalar(select(CoverAssetRow).where(CoverAssetRow.task_id == task_id))
                    if asset:
                        metadata = load_json_field(asset.recipe_json, {})
                        asset.recipe_json = json.dumps({**metadata, "error_code": code}, ensure_ascii=False)
                try:
                    result = self.tasks.submit_result(lease, stage="generate", status="cancelled" if code == "TASK_CANCELLED" else "failed", error_code=code,
                        duration_ms=0, publish=publish_failure)
                    if not result.get("accepted"):
                        self.tasks.recover_worker_failure(lease, stage="generate", error_code=code)
                except Exception:
                    self.tasks.recover_worker_failure(lease, stage="generate", error_code=code)
        finally:
            with self.lock: self.active.discard(task_id)

    def _compute(self, task_id, lease=None):
        with self.db.session() as sess:
            turn = sess.get(PlanningDialogueRow, task_id)
            if turn:
                self.draft(sess, turn.draft_id, turn.owner_user_id)
                sess.expunge(turn)
            else:
                asset = sess.execute(select(CoverAssetRow).where(CoverAssetRow.task_id == task_id)).scalar_one()
                target = self.target(sess, asset.target_kind, asset.target_id, asset.owner_user_id)
                from .cover_policy import require_eligible
                require_eligible(sess, asset.target_kind, asset.target_id, asset.owner_user_id)
                title = target.title_ref
                sess.expunge(asset)
        return self._dialogue(turn) if turn else self._draw(asset, title, lease)

    def _dialogue(self, turn):
        base = load_json_field(turn.base_json, {})
        with self.db.session() as sess:
            draft = self.draft(sess, turn.draft_id, turn.owner_user_id)
            kind = draft.draft_kind
            source = sess.execute(select(PlanningTaskStateRow).where(
                PlanningTaskStateRow.draft_id == turn.draft_id, PlanningTaskStateRow.owner_user_id == turn.owner_user_id)
                .order_by(PlanningTaskStateRow.updated_at.desc())).scalars().first()
            profile = load_json_field(source.intake_json, {}) if source else {}
            materials = load_json_field(source.material_ids_json, []) if source else []
            previous = sess.execute(select(PlanningDialogueRow).where(PlanningDialogueRow.draft_id == turn.draft_id,
                PlanningDialogueRow.created_at < turn.created_at).order_by(PlanningDialogueRow.created_at.desc()).limit(6)).scalars().all()
            history = []
            # Resume complete PydanticAI turns, including tool returns and retry parts.
            # Stored history grows by whole turns; avoid cutting a call/return pair.
            if previous and len(previous[0].messages_json) <= 32000:
                try: history = ModelMessagesTypeAdapter.validate_json(previous[0].messages_json)
                except ValueError: history = []
            if not history:
                for old in reversed(previous):
                    result = load_json_field(old.result_json, {})
                    if result.get("answer"):
                        history.extend([ModelRequest(parts=[UserPromptPart(content=old.user_content)]),
                                        ModelResponse(parts=[TextPart(content=result["answer"])])])
        adapter = self.planning._require_model_adapter(turn.owner_user_id, turn.task_id)
        catalog = node_catalog(base, kind)
        def get_current_plan() -> dict:
            """Read the current turn's immutable plan baseline and node references."""
            return {"content": base, "nodes": catalog}
        def validate_plan_changes(operations: list[NodeOperation]) -> dict:
            """Validate typed edits against this plan without writing the draft."""
            try:
                candidate = apply_operations(base, kind, operations)
                return {"valid": True, "changes": differences(base, candidate, kind)}
            except Exception as exc:
                return {"valid": False, "error_code": getattr(exc, "code", "OUTPUT_INVALID")}
        # Tool annotations are evaluated by PydanticAI in module globals.
        tools = [get_current_plan]
        if materials:
            from ..agents.tools import make_search_materials_tool
            tools.append(make_search_materials_tool(self.planning.retrieval, owner_user_id=turn.owner_user_id, material_ids=materials))
        prompt = self.prompts.resolve_for_task(turn.task_id, "planning_dialogue_v1")["template_snapshot"]
        context = json.dumps({"kind": kind, "plan": base, "nodes": catalog, "profile": profile,
                              "selected_node": turn.selected_node}, ensure_ascii=False)
        if len(context) > 48000:
            context = json.dumps({"kind": kind, "nodes": catalog, "profile": profile,
                "selected_node": turn.selected_node, "full_plan_available_via": "get_current_plan"},
                ensure_ascii=False)
        run = dialogue_agent.answer(adapter, DialogueInput.from_messages([{"role": "system", "content": prompt},
            {"role": "user", "content": context+"\n用户修改要求："+turn.user_content}],history),
            deps_for_source(adapter), tools=tools)
        answer = run.payload
        result = answer.model_dump()
        if answer.action == "revision_request":
            def validator(ctx, output):
                try: apply_operations(base, kind, output.operations)
                except Exception as exc: raise ModelRetry(f"修改操作无效：{getattr(exc, 'code', 'OUTPUT_INVALID')}；检查节点引用、完整子树和先修关系。") from None
                return output
            prompt = self.prompts.resolve_for_task(turn.task_id, "planning_revise_v1")["template_snapshot"]
            revised = revision_agent.revise(adapter, RevisionInput.from_messages([{"role": "system", "content": prompt},
                {"role": "user", "content": context+"\n明确要求："+(answer.instruction or turn.user_content)}]),
                deps_for_source(adapter),tools=[get_current_plan, validate_plan_changes], output_validator=validator)
            candidate = apply_operations(base, kind, revised.payload.operations)
            result.update(candidate=candidate, changes=differences(base, candidate, kind), summary=revised.payload.summary)
        return {"result": result, "messages_json": run.messages_json}

    def _draw(self, asset, title, lease=None):
        metadata = load_json_field(asset.recipe_json, {})
        if metadata.get("automatic"):
            from .automatic_cover import draw
            return draw(self, asset, metadata, lease)
        if metadata.get("title") is not None and metadata["title"] != title:
            raise error("OUTPUT_INVALID", "知识主题已变更")
        if metadata.get("reuse_asset_id") and asset.image_bytes:
            return {"image": asset.image_bytes, "recipe": metadata}
        if asset.method != "procedural":
            raise error("disclosure_required", "图片请求缺少明确授权")
        seed = int(sha256(asset.asset_id.encode()).hexdigest()[:8], 16)
        composition = "flow" if "python" in title.lower() else "branches" if any(x in title for x in ("树", "算法")) else "grid"
        recipe = InkRecipe(composition=composition)
        return {"image": render_ink(recipe, seed), "recipe": {**metadata, "method": "procedural",
            "requested_method": "procedural", "actual_method": "procedural", "fallback_reason": None, "error_code": None}}


from .adjustments import NodeOperation  # annotations used by tool schema generation

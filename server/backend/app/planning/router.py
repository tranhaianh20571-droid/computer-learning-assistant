"""切片 3 API 路由：规划任务、草稿、场景、总览、即时对话、教学门槛。

- 统一会话认证 + ownership；响应不含路径/文件名/密钥；
- 错误一律映射为受控错误码（PlanningError / WaitStateSignal 已映射）；
- 幂等键：规划任务与确认接口必填；
- 教学门槛：current_outline_version_id 为空时服务端禁止启动教学或 TTS。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Optional

import anyio
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..logging_audit.db import Database
from ..materials.chunk_index import ChunkIndexService
from ..prompts.service import PromptService
from ..tasks.service import TaskService
from ..logging_audit.repositories import PgTaskEventStore
from .chat import ChatService
from .drafts import DraftService
from .errors import PlanningError, error
from .models import (
    CourseRow,
    LearningRouteRow,
    PlanningDraftRow,
    PlanningViewStateRow,
    RESOLVED_SCOPES,
    SCENARIOS,
    load_json_field,
    utcnow,
)
from .overview import OverviewService
from .outputs import strip_internal_ids_in_place
from .retrieval import PlanningRetrievalService
from .scene import draft_scene, outline_scene, route_scene
from .service import PlanningService


class _ChatStreamingResponse(StreamingResponse):
    def __init__(self, meta, stream):
        self.stream = stream

        def sse():
            try:
                yield "event: meta\ndata: " + json.dumps(meta, ensure_ascii=False) + "\n\n"
                for event in stream:
                    yield "event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            finally:
                stream.close()

        self.sse = sse()
        super().__init__(self.sse, media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def listen_for_disconnect(self, receive):
        await super().listen_for_disconnect(receive)
        # Wake a worker blocked in next() before Starlette waits for its task group.
        self.stream.cancel()

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.stream.cancel()
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self._close)

    def _close(self):
        self.stream.close()
        self.sse.close()


class PlanningTaskIn(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=128)
    task_kind: str = Field(min_length=1, max_length=30)  # route_generate | outline_generate | planning_revise
    scenario: str = "self_study"
    planning_scope: str = "auto"
    signals: dict = Field(default_factory=dict)
    material_ids: list[str] = Field(default_factory=list)
    cover_auto_generate: bool = False
    target_course_id: str | None = None
    target_route_id: str | None = None
    source_route_node_id: str | None = None


class DraftSaveIn(BaseModel):
    revision: int = Field(ge=1)
    content: dict
    saved_state: str = "saved"


class DraftConfirmIn(BaseModel):
    revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)
    allow_duplicate: bool = False
    duplicate_fingerprint: str | None = Field(default=None, max_length=80)


class DraftDuplicateIn(BaseModel):
    revision: int = Field(ge=1)


class InputMatchIn(BaseModel):
    input: str = Field(min_length=1, max_length=4000)


class RefineDraftIn(BaseModel):
    revision: int = Field(ge=1)
    course_index: int | None = Field(default=None, ge=0)


class ScopeSwitchIn(BaseModel):
    cover_auto_generate: bool = False
    planning_scope: str = Field(min_length=1, max_length=10)
    signals: dict = Field(default_factory=dict)


class ViewstateIn(BaseModel):
    canvas: dict


class DecisionIn(BaseModel):
    background: bool = False
    resolved_scope: str | None = None
    decision: str | None = None
    # v2 生成前访谈：kind="intake" + answers=[{question_id, selections[], free_text?}]
    kind: str | None = None
    answers: list[dict] | None = None


class RouteShellIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    scenario: str = "self_study"
    input: str = Field(default="", max_length=4000)


class ResumePlanningIn(BaseModel):
    automatic: bool = False


class AnswerDraftIn(BaseModel):
    revision: int = Field(ge=0)
    answers: list[dict] = Field(default_factory=list, max_length=6)


class CourseShellIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    scenario: str = "self_study"
    source_route_version_id: str | None = None
    source_route_node_id: str | None = None


class ChatCreateIn(BaseModel):
    material_ids: list[str] = Field(default_factory=list)
    scenario: str = "self_study"


class ChatSendIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    use_search: bool = False
    idempotency_key: str = Field(min_length=1, max_length=128)


class ChatResumeIn(BaseModel):
    decision: str = Field(min_length=1, max_length=20)


class NavigationIn(BaseModel):
    route_id: str | None = None
    course_id: str | None = None
    unit_id: str | None = None
    lesson_id: str | None = None


def create_planning_router(
    *,
    db: Database,
    tasks: TaskService,
    events: PgTaskEventStore,
    prompts: PromptService,
    planning: PlanningService,
    drafts: DraftService,
    chat: ChatService,
    overview: OverviewService,
    deletion,
    resolve_user: Callable[[Request], Any],
    features=None,
    teaching=None,
) -> APIRouter:
    router = APIRouter(tags=["slice3"])

    def _user(request: Request):
        return resolve_user(request)

    # ------------------------------------------------------------------
    # 规划实体（壳）与任务创建（四入口）
    # ------------------------------------------------------------------
    @router.post("/api/planning/routes", status_code=201)
    def create_route(body: RouteShellIn, request: Request) -> dict:
        user = _user(request)
        if body.scenario not in SCENARIOS:
            raise error("invalid_field", "unknown scenario")
        route_id = f"route_{uuid.uuid4().hex[:12]}"
        with db.session() as sess:
            sess.add(
                LearningRouteRow(
                    route_id=route_id, owner_user_id=user.user_id,
                    scenario=body.scenario, title_ref=body.title,
                    input_ref=body.input,
                )
            )
            sess.commit()
        return {"route_id": route_id}

    @router.post("/api/planning/courses", status_code=201)
    def create_course(body: CourseShellIn, request: Request) -> dict:
        user = _user(request)
        if body.scenario not in SCENARIOS:
            raise error("invalid_field", "unknown scenario")
        course_id = f"crs_{uuid.uuid4().hex[:12]}"
        with db.session() as sess:
            sess.add(
                CourseRow(
                    course_id=course_id, owner_user_id=user.user_id,
                    scenario=body.scenario, title_ref=body.title,
                    source_route_version_id=body.source_route_version_id,
                    source_route_node_id=body.source_route_node_id,
                )
            )
            sess.commit()
        return {"course_id": course_id}

    @router.get("/api/planning/courses")
    def list_courses(request: Request) -> dict:
        """当前用户的课程列表（owned、未删除），含确认状态/进度与下一讲提示。"""
        user = _user(request)
        return {"data": overview.list_course_summaries(actor_id=user.user_id)}

    @router.get("/api/planning/library")
    def list_library(request: Request) -> dict:
        return {"data": overview.list_library(actor_id=_user(request).user_id)}

    @router.post("/api/planning/input-matches")
    def input_matches(body: InputMatchIn, request: Request) -> dict:
        return {"matches": overview.input_matches(body.input, actor_id=_user(request).user_id)}

    @router.post("/api/planning/tasks", status_code=201)
    def create_planning_task(body: PlanningTaskIn, request: Request) -> dict:
        user = _user(request)
        result = planning.create_planning_task(
            owner_user_id=user.user_id,
            task_kind=body.task_kind,
            scenario=body.scenario,
            planning_scope=body.planning_scope,
            material_ids=body.material_ids,
            target_id=body.target_course_id or body.target_route_id,
            source_route_node_id=body.source_route_node_id,
            signals=None,
            idempotency_key=body.idempotency_key,
        )
        # signals 以字段传入（范围解析纯函数在 worker 内使用）
        if body.signals:
            from .models import PlanningTaskStateRow

            with db.session() as sess:
                row = sess.get(PlanningTaskStateRow, result["task_id"])
                if row is not None:
                    row.scope_signals_json = json.dumps(body.signals, ensure_ascii=False)
                    sess.commit()
        if body.cover_auto_generate and features:
            from .models import PlanningTaskStateRow
            with db.session() as sess:
                row = sess.get(PlanningTaskStateRow, result["task_id"])
                target_kind, target_id = row.target_kind, row.target_id
            if target_id:
                try: result["cover"] = features.enqueue_automatic_cover(target_kind, target_id, user.user_id, authorized=True)
                except Exception: result["cover_status"] = "deferred"
        return result

    @router.get("/api/planning/current")
    def current_planning(request: Request) -> dict:
        return planning.workspace.current(_user(request).user_id)

    @router.post("/api/planning/tasks/{task_id}/resume")
    def resume_planning(task_id: str, body: ResumePlanningIn, request: Request) -> dict:
        return planning.workspace.resume(task_id, _user(request).user_id, automatic=body.automatic)

    @router.post("/api/planning/tasks/{task_id}/abandon")
    def abandon_planning(task_id: str, request: Request) -> dict:
        return planning.workspace.abandon(task_id, _user(request).user_id)

    @router.put("/api/planning/tasks/{task_id}/answer-draft")
    def save_answer_draft(task_id: str, body: AnswerDraftIn, request: Request) -> dict:
        return planning.workspace.save_answers(task_id, _user(request).user_id, body.revision, body.answers)

    @router.post("/api/planning/tasks/{task_id}/run")
    def run_planning_task(task_id: str, request: Request) -> dict:
        user = _user(request)
        planning.get_planning_state(task_id, actor_id=user.user_id)  # ownership
        # 用户显式重试：failed/expired 任务先重排队（generation+1、刷新截止），
        # 否则 claim 会以 TASK_NOT_CLAIMABLE / TASK_EXPIRED 直接失败。已终态成功的任务不改。
        tasks.requeue_for_retry(task_id, actor_id=user.user_id)
        return planning.run_task(task_id)

    @router.get("/api/planning/tasks/{task_id}")
    def get_planning_task(task_id: str, request: Request) -> dict:
        user = _user(request)
        return planning.get_planning_state(task_id, actor_id=user.user_id)

    @router.post("/api/planning/tasks/{task_id}/decision")
    def decide(task_id: str, body: DecisionIn, request: Request) -> dict:
        user = _user(request)
        if body.kind == "intake" or body.answers is not None:
            result = planning.decide_intake(task_id, actor_id=user.user_id, answers=body.answers or [])
            tasks.requeue_after_wait(task_id, actor_id=user.user_id)
            if body.background:
                return planning.workspace.resume(task_id, user.user_id)
            return result | planning.run_task(task_id)
        if body.resolved_scope is not None:
            result = planning.decide_scope(task_id, actor_id=user.user_id, resolved_scope=body.resolved_scope)
            tasks.requeue_after_wait(task_id, actor_id=user.user_id)
            if body.background:
                return planning.workspace.resume(task_id, user.user_id)
            return result | planning.run_task(task_id)
        if body.decision is not None:
            result = planning.decide_search(task_id, actor_id=user.user_id, decision=body.decision)
            tasks.requeue_after_wait(task_id, actor_id=user.user_id)
            if body.background:
                return planning.workspace.resume(task_id, user.user_id)
            return result | planning.run_task(task_id)
        raise error("invalid_field", "decision payload required")

    # ------------------------------------------------------------------
    # 草稿
    # ------------------------------------------------------------------
    @router.get("/api/planning/drafts/{draft_id}")
    def get_draft(draft_id: str, request: Request) -> dict:
        user = _user(request)
        return drafts.get_draft(draft_id, actor_id=user.user_id)

    @router.put("/api/planning/drafts/{draft_id}")
    def save_draft(draft_id: str, body: DraftSaveIn, request: Request) -> dict:
        user = _user(request)
        try:
            return drafts.save_draft(
                draft_id, actor_id=user.user_id,
                revision=body.revision, content=body.content,
                saved_state=body.saved_state,
            )
        except PlanningError:
            raise
        except Exception:  # noqa: BLE001 - 保存失败对前端可见
            drafts.mark_save_failed(draft_id, actor_id=user.user_id)
            raise

    @router.post("/api/planning/drafts/{draft_id}/confirm")
    def confirm_draft(draft_id: str, body: DraftConfirmIn, request: Request) -> dict:
        user = _user(request)
        result = drafts.confirm_draft(
            draft_id, actor_id=user.user_id,
            revision=body.revision, idempotency_key=body.idempotency_key,
            allow_duplicate=body.allow_duplicate, duplicate_fingerprint=body.duplicate_fingerprint,
        )
        if teaching and teaching.server_managed:
            item = drafts.get_draft(draft_id, actor_id=user.user_id)
            if item["draft_kind"] == "course_outline":
                try:
                    teaching.preparation.prepare_course(item["target_id"],user.user_id)
                except Exception:
                    result["preparation_status"] = "deferred"
            elif item["draft_kind"]=="route":
                try:teaching.preparation.prepare_owner(user.user_id)
                except Exception:result["preparation_status"]="deferred"
        if features:
            from ..logging_audit.contracts import normalize_error_code
            item = drafts.get_draft(draft_id, actor_id=user.user_id)
            kind = "route" if item["draft_kind"] == "route" else "course"
            target_id = item["target_id"]
            try:
                cover = (features.cover(kind, target_id, user.user_id) if result.get("deduped")
                         else features.enqueue_cover(kind, target_id, user.user_id, "automatic", automatic=True))
                result["covers"] = [{"target_kind": kind, "target_id": target_id, **cover}]
            except Exception as exc:
                result["covers"] = [{"target_kind": kind, "target_id": target_id, "status": "failed",
                    "error_code": normalize_error_code(getattr(exc, "code", None)),
                    "requested_method": None, "actual_method": None, "fallback_reason": None}]
        return result

    @router.post("/api/planning/drafts/{draft_id}/duplicates")
    def duplicate_check(draft_id: str, body: DraftDuplicateIn, request: Request) -> dict:
        return drafts.duplicate_check(draft_id, actor_id=_user(request).user_id, revision=body.revision)

    @router.post("/api/planning/{target_kind}/{target_id}/edit-draft")
    def edit_confirmed(target_kind: str, target_id: str, request: Request) -> dict:
        return drafts.edit_confirmed(target_kind, target_id, actor_id=_user(request).user_id)

    @router.post("/api/planning/drafts/{draft_id}/regenerate")
    def regenerate_draft(draft_id: str, request: Request) -> dict:
        """对现有目标重新生成候选（新建规划任务；不覆盖更高 revision 草稿）。"""
        user = _user(request)
        from .locking import planning_target
        with db.session() as sess:
            draft = sess.get(PlanningDraftRow, draft_id)
            if draft is None or draft.owner_user_id != user.user_id:
                raise error("draft_not_found", status=404)
            planning_target(sess, draft.draft_kind, draft.target_id, user.user_id, mutable=True)
            task_kind = "route_generate" if draft.draft_kind == "route" else "outline_generate"
            target_course_id = draft.target_id if draft.draft_kind == "course_outline" else None
        return planning.create_planning_task(
            owner_user_id=user.user_id,
            task_kind=task_kind,
            scenario="self_study",
            planning_scope="auto",
            target_course_id=target_course_id,
            idempotency_key=f"regen:{draft_id}:{uuid.uuid4().hex[:8]}",
        )

    @router.post("/api/planning/drafts/{draft_id}/refine")
    def refine_draft(draft_id: str, body: RefineDraftIn, request: Request) -> dict:
        user = _user(request)
        draft = drafts.get_draft(draft_id, actor_id=user.user_id)
        if draft["revision"] != body.revision:
            raise error("DRAFT_REVISION_MISMATCH", status=409)
        if draft["draft_kind"] != "route":
            raise error("invalid_field")
        courses = [c for st in draft["content"].get("stages", []) for c in st["courses"]]
        if body.course_index is not None and body.course_index >= len(courses):
            raise error("invalid_field")
        result = planning.create_planning_task(
            owner_user_id=user.user_id, task_kind="planning_revise", scenario="self_study",
            planning_scope="route", target_id=draft["target_id"],
            idempotency_key=f"refine:{draft_id}:{body.revision}:{body.course_index}",
        )
        from .models import PlanningTaskStateRow
        with db.session() as sess:
            state = sess.get(PlanningTaskStateRow, result["task_id"])
            source = sess.execute(select(PlanningTaskStateRow).where(
                PlanningTaskStateRow.draft_id == draft_id,
                PlanningTaskStateRow.owner_user_id == user.user_id,
                PlanningTaskStateRow.task_id != result["task_id"],
            ).order_by(PlanningTaskStateRow.updated_at.desc())).scalars().first()
            if source:
                state.intake_json = source.intake_json
                state.material_ids_json = source.material_ids_json
            state.scope_signals_json = json.dumps({"refine_draft": {
                "draft_id": draft_id, "revision": body.revision, "course_index": body.course_index,
            }})
            state.draft_id = draft_id
            sess.commit()
        return result

    @router.post("/api/planning/drafts/{draft_id}/scope")
    def switch_scope(draft_id: str, body: ScopeSwitchIn, request: Request) -> dict:
        """切换范围：保留输入，新草稿承接范围变化。"""
        user = _user(request)
        if body.planning_scope not in ("auto", "course", "route"):
            raise error("invalid_field", "unknown planning_scope")
        with db.session() as sess:
            draft = sess.get(PlanningDraftRow, draft_id)
            if draft is None or draft.owner_user_id != user.user_id:
                raise error("draft_not_found", status=404)
            from .locking import planning_target
            planning_target(sess, draft.draft_kind, draft.target_id, user.user_id, mutable=True)
            previous_content = load_json_field(draft.content_json, {})
            previous_kind = draft.draft_kind
            previous_base = draft.base_version_id
        new_kind = "route" if body.planning_scope == "route" else (
            "course_outline" if body.planning_scope == "course" else previous_kind
        )
        new_target_id = draft_target(draft_id, db)
        if new_kind != previous_kind:
            with db.session() as sess:
                original, _ = planning_target(sess, previous_kind, new_target_id, user.user_id, mutable=True)
                if new_kind == "course_outline":
                    new_target_id = f"crs_{uuid.uuid4().hex[:12]}"
                    sess.add(CourseRow(course_id=new_target_id,owner_user_id=user.user_id,scenario=original.scenario,
                        title_ref=previous_content.get("title") or original.title_ref))
                    previous_content = dict(previous_content)
                    previous_content["units"] = [u for st in previous_content.get("stages",[]) for c in st.get("courses",[]) for u in c.get("units",[])]
                    previous_content["prerequisites"] = []
                else:
                    new_target_id = f"route_{uuid.uuid4().hex[:12]}"
                    sess.add(LearningRouteRow(route_id=new_target_id,owner_user_id=user.user_id,scenario=original.scenario,
                        title_ref=previous_content.get("title") or original.title_ref))
                    previous_content = {"title":previous_content.get("title") or original.title_ref,
                        "stages":[{"title":"课程规划","courses":[{"title":previous_content.get("title") or original.title_ref,
                        "units":previous_content.get("units",[]),"prerequisites":previous_content.get("prerequisites",[])}]}],"prerequisites":[]}
            previous_base = None
        new_draft = drafts.find_or_create_draft(
            owner_user_id=user.user_id,
            draft_kind=new_kind,
            target_id=new_target_id,
            base_version_id=previous_base,
            initial_content=previous_content,
        )
        cover = None
        if features and new_kind != previous_kind:
            source_kind = "route" if previous_kind == "route" else "course"
            target_kind = "route" if new_kind == "route" else "course"
            cover = features.reuse_cover(source_kind, draft.target_id, target_kind, new_target_id, user.user_id)
            if cover is None:
                cover = features.enqueue_cover(target_kind, new_target_id, user.user_id, "scope-local", automatic=True)
        return {"draft": new_draft, "previous_kind": previous_kind, "new_kind": new_kind, "cover": cover}

    def draft_target(draft_id: str, db: Database) -> str:
        with db.session() as sess:
            draft = sess.get(PlanningDraftRow, draft_id)
            return draft.target_id if draft else ""

    # ------------------------------------------------------------------
    # 场景（导图）与视图状态
    # ------------------------------------------------------------------
    @router.get("/api/routes/{route_id}/scene")
    def route_scene_api(route_id: str, request: Request, version_id: str | None = None) -> dict:
        user = _user(request)
        with db.session() as sess:
            route = sess.get(LearningRouteRow, route_id)
            if route is None or route.owner_user_id != user.user_id or route.deleted_at is not None:
                raise error("route_not_found", status=404)
            if not route.current_route_version_id:
                raise error("route_not_found", "no confirmed route version", status=404)
            selected = version_id or route.current_route_version_id
            from .models import RouteVersionRow
            version = sess.get(RouteVersionRow, selected)
            if version is None or version.route_id != route_id:
                raise error("route_not_found", status=404)
            data = _route_version_payload(sess, selected)
        result = route_scene(data)
        result["version_id"] = selected
        return result

    @router.get("/api/courses/{course_id}/scene")
    def course_scene_api(course_id: str, request: Request) -> dict:
        user = _user(request)
        with db.session() as sess:
            course = sess.get(CourseRow, course_id)
            if course is None or course.owner_user_id != user.user_id or course.deleted_at is not None:
                raise error("course_not_found", status=404)
            if not course.current_outline_version_id:
                raise error("course_not_found", "no confirmed outline version", status=404)
            data = _outline_version_payload(sess, course.current_outline_version_id)
        result = outline_scene(data)
        result["version_id"] = course.current_outline_version_id
        return result

    @router.get("/api/planning/drafts/{draft_id}/scene")
    def draft_scene_api(draft_id: str, request: Request) -> dict:
        """草稿预览场景：生成后即可看导图，不必先确认草稿。

        读路径统一兜底剥除标题里的内部 id：存量草稿（剥除逻辑上线前生成）
        也能正常展示，不阻塞预览。目标标题作根卡回退（大标题/课程卡）。
        """
        user = _user(request)
        with db.session() as sess:
            draft = sess.get(PlanningDraftRow, draft_id)
            if draft is None or draft.owner_user_id != user.user_id:
                raise error("draft_not_found", status=404)
            content = load_json_field(draft.content_json, {})
            target = sess.get(LearningRouteRow, draft.target_id) if draft.draft_kind in ("route", "route_generate") else sess.get(CourseRow, draft.target_id)
            title = (target.title_ref or "") if target else ""
        strip_internal_ids_in_place(content)
        return draft_scene(draft.draft_kind, content, title=title)

    def validate_view_target(sess, target_kind, target_id, actor_id):
        base_id = target_id.split(":", 1)[0]
        model = {"route": LearningRouteRow, "course": CourseRow, "draft": PlanningDraftRow}.get(target_kind)
        target = sess.get(model, base_id) if model else None
        if target is None or target.owner_user_id != actor_id or getattr(target, "deleted_at", None):
            raise error("draft_not_found", status=404)
        if target_kind == "draft":
            parent_model = LearningRouteRow if target.draft_kind == "route" else CourseRow
            parent = sess.get(parent_model, target.target_id)
            if parent is None or parent.owner_user_id != actor_id or parent.deleted_at:
                raise error("draft_not_found", status=404)

    @router.get("/api/planning/viewstates/{target_kind}/{target_id}")
    def get_viewstate(target_kind: str, target_id: str, request: Request) -> dict:
        user = _user(request)
        with db.session() as sess:
            validate_view_target(sess, target_kind, target_id, user.user_id)
            row = sess.execute(
                select(PlanningViewStateRow).where(
                    PlanningViewStateRow.owner_user_id == user.user_id,
                    PlanningViewStateRow.target_kind == target_kind,
                    PlanningViewStateRow.target_id == target_id,
                )
            ).scalars().first()
            return {"canvas": load_json_field(row.canvas_json, {}) if row else {}}

    @router.put("/api/planning/viewstates/{target_kind}/{target_id}")
    def put_viewstate(target_kind: str, target_id: str, body: ViewstateIn, request: Request) -> dict:
        user = _user(request)
        with db.session() as sess:
            validate_view_target(sess, target_kind, target_id, user.user_id)
            row = sess.execute(
                select(PlanningViewStateRow).where(
                    PlanningViewStateRow.owner_user_id == user.user_id,
                    PlanningViewStateRow.target_kind == target_kind,
                    PlanningViewStateRow.target_id == target_id,
                )
            ).scalars().first()
            if row is None:
                row = PlanningViewStateRow(
                    view_state_id=f"vs_{uuid.uuid4().hex[:12]}",
                    owner_user_id=user.user_id,
                    target_kind=target_kind,
                    target_id=target_id,
                )
                sess.add(row)
            row.canvas_json = json.dumps(body.canvas, ensure_ascii=False)
            row.updated_at = utcnow()
            sess.commit()
        return {"saved": True}

    # ------------------------------------------------------------------
    # 总览
    # ------------------------------------------------------------------
    @router.get("/api/routes/{route_id}/overview")
    def route_overview(route_id: str, request: Request, version_id: str | None = None) -> dict:
        user = _user(request)
        return overview.route_overview(route_id, actor_id=user.user_id, version_id=version_id)

    @router.get("/api/courses/{course_id}/overview")
    def course_overview(course_id: str, request: Request, route_id: str | None = None,
                        route_version_id: str | None = None) -> dict:
        user = _user(request)
        result = overview.course_overview(course_id, actor_id=user.user_id,
                                        route_id=route_id, route_version_id=route_version_id)
        if teaching and teaching.server_managed:
            try:
                teaching.preparation.prepare_course(course_id,user.user_id)
            except Exception:
                result["preparation_status"] = "deferred"
        return result

    @router.post("/api/routes/{route_id}/courses/{course_id}/detach")
    def detach_course(route_id: str, course_id: str, request: Request) -> dict:
        return deletion.detach_course(route_id, course_id, actor_id=_user(request).user_id)

    # 教学门槛与启动教学路由归切片 4（teaching router）。

    @router.delete("/api/planning/routes/{route_id}")
    def delete_route(route_id: str, request: Request) -> dict:
        """删除路线：课程保留，编排解除，写墓碑与审计。"""
        user = _user(request)
        return deletion.delete_route(route_id, actor_id=user.user_id)

    @router.delete("/api/planning/courses/{course_id}")
    def delete_course(course_id: str, request: Request) -> dict:
        """删除课程：清理草稿/版本/讲次/教学产物，教学会话软删。"""
        user = _user(request)
        return deletion.delete_course(course_id, actor_id=user.user_id)
    # ------------------------------------------------------------------
    # 导航状态
    # ------------------------------------------------------------------
    @router.get("/api/navigation")
    def get_navigation(request: Request) -> dict:
        user = _user(request)
        return overview.get_navigation(actor_id=user.user_id)

    @router.put("/api/navigation")
    def put_navigation(body: NavigationIn, request: Request) -> dict:
        user = _user(request)
        return overview.update_navigation(
            actor_id=user.user_id, route_id=body.route_id, course_id=body.course_id,
            unit_id=body.unit_id, lesson_id=body.lesson_id,
        )

    # ------------------------------------------------------------------
    # 即时文字对话
    # ------------------------------------------------------------------
    @router.post("/api/chat", status_code=201)
    def create_chat(body: ChatCreateIn, request: Request) -> dict:
        user = _user(request)
        return chat.create_conversation(
            owner_user_id=user.user_id, scenario=body.scenario,
            material_ids=body.material_ids,
        )

    @router.get("/api/chat/{conversation_id}")
    def get_chat(conversation_id: str, request: Request) -> dict:
        user = _user(request)
        return chat.get_conversation(conversation_id, actor_id=user.user_id)

    @router.post("/api/chat/{conversation_id}/messages")
    def send_chat(conversation_id: str, body: ChatSendIn, request: Request) -> dict:
        user = _user(request)
        return chat.send_message(
            conversation_id, actor_id=user.user_id, content=body.content,
            use_search=body.use_search, idempotency_key=body.idempotency_key,
        )

    @router.post("/api/chat/{conversation_id}/messages/stream")
    def send_chat_stream(conversation_id: str, body: ChatSendIn, request: Request):
        """流式即时回答：SSE（meta → delta* → done | error | wait）。"""
        user = _user(request)
        meta, gen = chat.stream_answer(
            conversation_id, actor_id=user.user_id, content=body.content,
            use_search=body.use_search, idempotency_key=body.idempotency_key,
        )
        return _ChatStreamingResponse(meta, gen)

    @router.post("/api/chat/{conversation_id}/resume")
    def resume_chat(conversation_id: str, body: ChatResumeIn, request: Request) -> dict:
        user = _user(request)
        return chat.resume_message(conversation_id, actor_id=user.user_id, decision=body.decision)

    return router


# ---------------------------------------------------------------------------
# 版本结构读取（总览/场景共用「同版」口径）
# ---------------------------------------------------------------------------


def _route_version_payload(sess, route_version_id: str) -> dict:
    from .models import LearningRouteRow, RouteVersionRow
    from .library import LibrarySnapshot
    version = sess.get(RouteVersionRow, route_version_id)
    route = sess.get(LearningRouteRow, version.route_id) if version else None
    if route is None or route.deleted_at is not None:
        raise error("route_not_found", status=404)
    library = LibrarySnapshot(sess, route.owner_user_id)
    overview = library.route(route.route_id, route_version_id, False)
    stages, visible = [], set()
    for stage in overview["stages"]:
        courses = []
        for course in stage["courses"]:
            visible.add(course["node_id"])
            units = [{"id": u["unit_id"], "title": u["title"], "lessons": [
                {"id": l["lesson_id"], "title": l["title"], "goal": l["goal"]} for l in u["lessons"]
            ]} for u in course["units"]]
            edges = library.lesson_edges[course["current_version_id"]]
            courses.append({"id": course["node_id"], "course_id": course["course_id"],
                "title": course["title"], "goal": course.get("goal_snapshot", ""), "units": units,
                "prerequisites": [{"from": e.from_lesson_id, "to": e.to_lesson_id} for e in edges]})
        stages.append({"id": stage["stage_id"], "title": stage["title"], "courses": courses})
    return {"title": overview["title"], "version": version, "stages": stages,
        "prerequisites": [{"from": e.from_node_id, "to": e.to_node_id}
            for e in library.route_edges[route_version_id] if e.from_node_id in visible and e.to_node_id in visible]}


def _outline_version_payload(sess, outline_version_id: str) -> dict:
    from .models import CourseRow, CourseUnitRow, LessonPrerequisiteEdgeRow, LessonRow, OutlineVersionRow

    version = sess.get(OutlineVersionRow, outline_version_id)
    unit_rows = (
        sess.execute(
            select(CourseUnitRow)
            .where(CourseUnitRow.outline_version_id == outline_version_id)
            .order_by(CourseUnitRow.order_index)
        ).scalars().all()
    )
    lesson_rows = (
        sess.execute(
            select(LessonRow).where(LessonRow.outline_version_id == outline_version_id)
        ).scalars().all()
    )
    edges = (
        sess.execute(
            select(LessonPrerequisiteEdgeRow).where(
                LessonPrerequisiteEdgeRow.outline_version_id == outline_version_id
            )
        ).scalars().all()
    )
    units = []
    for unit in unit_rows:
        lessons = []
        for lesson in sorted([l for l in lesson_rows if l.unit_id == unit.unit_id], key=lambda l: l.order_index):
            lessons.append({"id": lesson.lesson_id, "title": lesson.goal_ref or lesson.lesson_id})
        units.append({"id": unit.unit_id, "title": unit.title_ref, "lessons": lessons})
    prerequisites = [{"from": e.from_lesson_id, "to": e.to_lesson_id} for e in edges]
    course = sess.get(CourseRow, version.course_id) if version else None
    return {
        "title": (version.title_snapshot if version else "") or (course.title_ref if course else "") or "",
        "version": version, "units": units, "prerequisites": prerequisites,
    }


__all__ = ["create_planning_router"]

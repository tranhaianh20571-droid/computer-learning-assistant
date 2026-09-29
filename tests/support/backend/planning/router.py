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

from fastapi import APIRouter, Request
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


class PlanningTaskIn(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=128)
    task_kind: str = Field(min_length=1, max_length=30)  # route_generate | outline_generate | planning_revise
    scenario: str = "self_study"
    planning_scope: str = "auto"
    signals: dict = Field(default_factory=dict)
    material_ids: list[str] = Field(default_factory=list)
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


class ScopeSwitchIn(BaseModel):
    planning_scope: str = Field(min_length=1, max_length=10)
    signals: dict = Field(default_factory=dict)


class ViewstateIn(BaseModel):
    canvas: dict


class DecisionIn(BaseModel):
    resolved_scope: str | None = None
    decision: str | None = None
    # v2 生成前访谈：kind="intake" + answers=[{question_id, selections[], free_text?}]
    kind: str | None = None
    answers: list[dict] | None = None


class RouteShellIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    scenario: str = "self_study"


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
        return result

    @router.post("/api/planning/tasks/{task_id}/run")
    def run_planning_task(task_id: str, request: Request) -> dict:
        user = _user(request)
        planning.get_planning_state(task_id, actor_id=user.user_id)  # ownership
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
            return result | planning.run_task(task_id)
        if body.resolved_scope is not None:
            result = planning.decide_scope(task_id, actor_id=user.user_id, resolved_scope=body.resolved_scope)
            tasks.requeue_after_wait(task_id, actor_id=user.user_id)
            return result | planning.run_task(task_id)
        if body.decision is not None:
            result = planning.decide_search(task_id, actor_id=user.user_id, decision=body.decision)
            tasks.requeue_after_wait(task_id, actor_id=user.user_id)
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
        return drafts.confirm_draft(
            draft_id, actor_id=user.user_id,
            revision=body.revision, idempotency_key=body.idempotency_key,
        )

    @router.post("/api/planning/drafts/{draft_id}/regenerate")
    def regenerate_draft(draft_id: str, request: Request) -> dict:
        """对现有目标重新生成候选（新建规划任务；不覆盖更高 revision 草稿）。"""
        user = _user(request)
        with db.session() as sess:
            draft = sess.get(PlanningDraftRow, draft_id)
            if draft is None or draft.owner_user_id != user.user_id:
                raise error("draft_not_found", status=404)
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
            previous_content = load_json_field(draft.content_json, {})
            previous_kind = draft.draft_kind
            previous_base = draft.base_version_id
        new_kind = "route" if body.planning_scope == "route" else (
            "course_outline" if body.planning_scope == "course" else previous_kind
        )
        new_draft = drafts.find_or_create_draft(
            owner_user_id=user.user_id,
            draft_kind=new_kind,
            target_id=draft_target(draft_id, db),
            base_version_id=previous_base,
            initial_content=previous_content,
        )
        return {"draft": new_draft, "previous_kind": previous_kind, "new_kind": new_kind}

    def draft_target(draft_id: str, db: Database) -> str:
        with db.session() as sess:
            draft = sess.get(PlanningDraftRow, draft_id)
            return draft.target_id if draft else ""

    # ------------------------------------------------------------------
    # 场景（导图）与视图状态
    # ------------------------------------------------------------------
    @router.get("/api/routes/{route_id}/scene")
    def route_scene_api(route_id: str, request: Request) -> dict:
        user = _user(request)
        with db.session() as sess:
            route = sess.get(LearningRouteRow, route_id)
            if route is None or route.owner_user_id != user.user_id or route.deleted_at is not None:
                raise error("route_not_found", status=404)
            if not route.current_route_version_id:
                raise error("route_not_found", "no confirmed route version", status=404)
            data = _route_version_payload(sess, route.current_route_version_id)
        return route_scene(data)

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
        return outline_scene(data)

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

    @router.get("/api/planning/viewstates/{target_kind}/{target_id}")
    def get_viewstate(target_kind: str, target_id: str, request: Request) -> dict:
        user = _user(request)
        with db.session() as sess:
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
    def route_overview(route_id: str, request: Request) -> dict:
        user = _user(request)
        return overview.route_overview(route_id, actor_id=user.user_id)

    @router.get("/api/courses/{course_id}/overview")
    def course_overview(course_id: str, request: Request) -> dict:
        user = _user(request)
        return overview.course_overview(course_id, actor_id=user.user_id)

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
        import json as _json

        from fastapi.responses import StreamingResponse

        user = _user(request)
        meta, gen = chat.stream_answer(
            conversation_id, actor_id=user.user_id, content=body.content,
            use_search=body.use_search, idempotency_key=body.idempotency_key,
        )

        def sse():
            meta_line = "event: meta\ndata: " + _json.dumps(meta, ensure_ascii=False) + "\n\n"
            yield meta_line
            for evt in gen:
                yield "event: " + evt["type"] + "\ndata: " + _json.dumps(evt, ensure_ascii=False) + "\n\n"

        return StreamingResponse(
            sse(), media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @router.post("/api/chat/{conversation_id}/resume")
    def resume_chat(conversation_id: str, body: ChatResumeIn, request: Request) -> dict:
        user = _user(request)
        return chat.resume_message(conversation_id, actor_id=user.user_id, decision=body.decision)

    return router


# ---------------------------------------------------------------------------
# 版本结构读取（总览/场景共用「同版」口径）
# ---------------------------------------------------------------------------


def _route_version_payload(sess, route_version_id: str) -> dict:
    from .models import (
        CourseRow, CourseUnitRow, LearningRouteRow, LessonRow,
        RouteCourseNodeRow, RoutePrerequisiteEdgeRow, RouteStageRow, RouteVersionRow,
    )

    version = sess.get(RouteVersionRow, route_version_id)
    route = sess.get(LearningRouteRow, version.route_id) if version else None
    stage_rows = (
        sess.execute(
            select(RouteStageRow)
            .where(RouteStageRow.route_version_id == route_version_id)
            .order_by(RouteStageRow.order_index)
        ).scalars().all()
    )
    node_rows = (
        sess.execute(
            select(RouteCourseNodeRow).where(RouteCourseNodeRow.route_version_id == route_version_id)
        ).scalars().all()
    )
    edges = (
        sess.execute(
            select(RoutePrerequisiteEdgeRow).where(RoutePrerequisiteEdgeRow.route_version_id == route_version_id)
        ).scalars().all()
    )
    stages = []
    for stage in stage_rows:
        courses = []
        for node in sorted([n for n in node_rows if n.stage_id == stage.stage_id], key=lambda n: n.order_index):
            # 场景节点以 route_course_nodes.node_id 为业务对象 ID（先修边引用同一 ID）；
            # 展示文本优先课程标题，缺省退回目标快照。
            # 课程当前大纲版本存在时附带单元/讲次（四层图：大标题→课程→单元→讲次）。
            course = sess.get(CourseRow, node.course_id)
            display = (course.title_ref if course else "") or node.goal_snapshot_ref or node.course_id
            units = []
            if course is not None and course.current_outline_version_id:
                unit_rows = sess.execute(
                    select(CourseUnitRow)
                    .where(CourseUnitRow.outline_version_id == course.current_outline_version_id)
                    .order_by(CourseUnitRow.order_index)
                ).scalars().all()
                lesson_rows = sess.execute(
                    select(LessonRow).where(LessonRow.outline_version_id == course.current_outline_version_id)
                ).scalars().all()
                for u in unit_rows:
                    units.append({
                        "id": u.unit_id,
                        "title": u.title_ref,
                        "lessons": [
                            {"id": l.lesson_id, "title": l.goal_ref or l.lesson_id, "goal": l.goal_ref}
                            for l in sorted(
                                [l for l in lesson_rows if l.unit_id == u.unit_id],
                                key=lambda l: l.order_index,
                            )
                        ],
                    })
            courses.append({"id": node.node_id, "course_id": node.course_id, "title": display, "units": units})
        stages.append({"id": stage.stage_id, "title": stage.title_ref, "courses": courses})
    prerequisites = [
        {"from": e.from_node_id, "to": e.to_node_id} for e in edges
    ]
    return {
        "title": (route.title_ref if route else "") or "",
        "version": version, "stages": stages, "prerequisites": prerequisites,
    }


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
        "title": (course.title_ref if course else "") or "",
        "version": version, "units": units, "prerequisites": prerequisites,
    }


__all__ = ["create_planning_router"]

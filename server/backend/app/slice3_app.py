"""切片 3 集成应用：在切片 2 应用上挂载规划/草稿/场景/总览/即时对话路由。"""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .logging_audit.repositories import PgAuditLog, PgTaskEventStore
from .planning.chat import ChatService
from .planning.drafts import DraftService
from .planning.errors import PlanningError, WaitStateSignal
from .planning.overview import OverviewService
from .planning.retrieval import PlanningRetrievalService
from .planning.router import create_planning_router
from .planning.service import PlanningService
from .prompts.service import PromptService
from .slice2_app import Slice2State, create_slice2_app
from .tasks.service import TaskService


class Slice3State:
    def __init__(
        self,
        db,
        audit: Optional[PgAuditLog] = None,
        *,
        model_adapter_factory: Optional[Any] = None,
        search_adapter_factory: Optional[Any] = None,
    ) -> None:
        self.db = db
        self.audit = audit
        self.events = PgTaskEventStore(db)
        self.tasks = TaskServiceShim(db, self.events)
        self.prompts = PromptServiceShim(db)
        self.retrieval = PlanningRetrievalService(db)
        self.drafts = DraftService(db, audit)
        self.planning = PlanningService(
            db, self.tasks, self.events, self.prompts, self.drafts, self.retrieval,
            audit=audit,
            model_adapter_factory=model_adapter_factory,
            search_adapter_factory=search_adapter_factory,
        )
        self.chat = ChatService(
            db, self.tasks, self.events, self.prompts, self.retrieval,
            model_adapter_factory=model_adapter_factory,
            search_adapter_factory=search_adapter_factory,
        )
        self.overview = OverviewService(db)


class TaskServiceShim(TaskService):
    """复用切片 0 任务围栏；切片 3 仅消费。"""


class PromptServiceShim(PromptService):
    """复用切片 0 提示词绑定；切片 3 仅消费。"""


# ---------------------------------------------------------------------------
# 仅限本地开发走查的合成适配器（LEARNING_DEV_SYNTHETIC_MODEL=1 显式启用）。
# - 不产生任何外发请求、不使用任何凭据；输出为固定合成样本（含长中文标题，
#   供 T02 溢出阈值走查）；
# - 生产路径不得启用；真实模型/搜索调用必须走切片 1 适配器与外发核验。
# ---------------------------------------------------------------------------


class DevSyntheticModelAdapter:
    kind = "content_model"
    protocol = "openai"
    capability_status = {
        "text": "available", "image": "unavailable", "tool_call": "available",
        "json_schema": "available", "streaming": "unavailable", "cancel": "unavailable",
    }

    def probe(self, capability: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            capability=capability,
            state=self.capability_status.get(capability, "unavailable"),
        )

    def generate(self, messages, **options):
        import json as _json

        # 即时文字路径（plain_text=True）返回纯文字回答，不返回结构化 JSON
        if options.get("plain_text"):
            return {"content": "事务隔离级别描述的是并发事务之间的可见性约定："
                     "读未提交最低，读已提交避免脏读，可重复读避免不可重复读，"
                     "串行化最高但并发最差（合成走查回答，非真实模型）。"}
        last = ""
        for m in messages:
            if m.get("role") == "user":
                last = str(m.get("content", ""))
        if "outline" in last or "大纲" in last:
            payload = {
                "units": [
                    {"title": "单元一：数据库基础与 SQL 实践（长单元标题观察换行与省略号策略）",
                     "lessons": [
                         {"title": "第 1 讲：环境准备与第一个查询", "goal": "能执行 SELECT"},
                         {"title": "第 2 讲：表设计与约束（含中英文 Mixed Terms 混排观察）", "goal": "会建表"},
                     ]},
                    {"title": "单元二：事务与并发控制", "lessons": [
                         {"title": "第 3 讲：事务隔离级别与并发异常：脏读、不可重复读、幻读的判定（长中文讲次标题）", "goal": "理解隔离级别"},
                         {"title": "第 4 讲：索引与执行计划", "goal": "会读计划"},
                    ]},
                ],
                "prerequisites": [{"from_index": 0, "to_index": 2}],
            }
        else:
            payload = {
                "stages": [
                    {"title": "阶段一：基础入门（超长标题观察标点换行与两行截断策略）",
                     "courses": [
                         {"title": "数据库基础：关系模型与 SQL（Mixed Terms 混排观察）", "goal": "掌握关系模型"},
                         {"title": "SQL 实践与查询优化入门", "goal": "能写复杂查询"},
                     ]},
                    {"title": "阶段二：进阶与工程实践",
                     "courses": [
                         {"title": "事务与并发控制", "goal": "理解隔离级别"},
                         {"title": "索引结构、执行计划与调优", "goal": "会读执行计划"},
                     ]},
                ],
                "prerequisites": [{"from_index": 0, "to_index": 2}],
            }
        text = _json.dumps(payload, ensure_ascii=False)
        if "tools" in options:
            return {"content": "", "tool_arguments": text}
        return {"content": text}


class DevSyntheticSearchAdapter:
    kind = "search"
    protocol = "tavily_hikari"

    def search(self, query, **kwargs):
        return {
            "query_id": "qry_dev_synthetic",
            "results": [{
                "title": "合成搜索结果（本地走查，非真实联网）",
                "url": "https://example.com/synthetic",
                "content": "synthetic snippet for walkthrough",
                "fetched_at": "2026-09-27T00:00:00Z",
            }],
        }


def _dev_synthetic_factories() -> tuple[Optional[Any], Optional[Any]]:
    if os.environ.get("LEARNING_DEV_SYNTHETIC_MODEL") != "1":
        return None, None
    return (
        lambda owner, task_id: DevSyntheticModelAdapter(),
        lambda owner, task_id: DevSyntheticSearchAdapter(),
    )


def create_slice3_app(
    state: Optional[Slice3State] = None,
    slice2_state: Optional[Slice2State] = None,
    *,
    model_adapter_factory: Optional[Any] = None,
    search_adapter_factory: Optional[Any] = None,
) -> FastAPI:
    if model_adapter_factory is None or search_adapter_factory is None:
        dev_model, dev_search = _dev_synthetic_factories()
        model_adapter_factory = model_adapter_factory or dev_model
        search_adapter_factory = search_adapter_factory or dev_search
    app = create_slice2_app(state=slice2_state)
    if state is None:
        base = app.state.slice1
        state = Slice3State(
            base.db, base.audit,
            model_adapter_factory=model_adapter_factory,
            search_adapter_factory=search_adapter_factory,
        )
    app.state.slice3 = state

    def _resolve_user(request: Request):
        raw = request.headers.get("Authorization", "")
        token = raw[7:] if raw.startswith("Bearer ") else ""
        if not token:
            for part in request.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "session_token":
                    token = v
        from .auth.service import AuthError

        try:
            return app.state.slice0.auth.resolve_session(token)
        except AuthError as exc:
            raise HTTPException(
                status_code=exc.status, detail={"error_code": exc.code, "message": exc.message}
            )

    app.include_router(
        create_planning_router(
            db=state.db,
            tasks=state.tasks,
            events=state.events,
            prompts=state.prompts,
            planning=state.planning,
            drafts=state.drafts,
            chat=state.chat,
            overview=state.overview,
            resolve_user=_resolve_user,
        )
    )

    @app.exception_handler(PlanningError)
    async def planning_error(_: Request, exc: PlanningError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"error_code": exc.code, "message": exc.message}
        )

    @app.exception_handler(WaitStateSignal)
    async def wait_state_error(_: Request, exc: WaitStateSignal) -> JSONResponse:
        # 等待态不计为失败：200 语义由 API 层以 state 字段承载；异常路径给 409+状态
        return JSONResponse(
            status_code=409,
            content={
                "error_code": exc.error_code,
                "state": exc.wait_state,
                "message": exc.question or exc.detail,
            },
        )

    return app

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
        ocr_adapter_factory: Optional[Any] = None,
        tts_adapter_factory: Optional[Any] = None,
        storage=None,
        pipeline_holder: Optional[dict] = None,
    ) -> None:
        self.db = db
        self.audit = audit
        self.events = PgTaskEventStore(db)
        self.tasks = TaskServiceShim(db, self.events)
        self.prompts = PromptServiceShim(db)
        from .materials.storage import MaterialStorage

        self.storage = storage or MaterialStorage()
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
        # 切片 4：讲次教学（步骤生成/板书/TTS/检查点/追问/恢复）
        from .teaching.service import TeachingService

        self.teaching = TeachingService(
            db, self.tasks, self.events, self.prompts, self.storage,
            model_adapter_factory=model_adapter_factory,
            tts_adapter_factory=tts_adapter_factory,
        )
        # 切片 2 T03/T04：逐页解析 + OCR 管线（适配器经工厂注入，真实凭据为人工前置）
        from .materials.ocr import OcrPipelineService
        from .materials.chunk_index import ChunkIndexService
        from .slice1.disclosure_service import DisclosureService

        self.chunk_index = ChunkIndexService(db)
        self.ocr_pipeline = OcrPipelineService(
            db, self.tasks, self.events, self.chunk_index,
            storage=self.storage,
            ocr_adapter_factory=ocr_adapter_factory,
            disclosures=DisclosureService(db, audit),
        )
        # 资料路由经 holder 惰性取管线（slice2 装配早于 slice3）
        self.pipeline_holder = pipeline_holder if pipeline_holder is not None else {"pipeline": None}
        self.pipeline_holder["pipeline"] = self.ocr_pipeline


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
        if "follow-up question" in last:
            payload = {
                "answer": "这是针对追问的合成回答：概念按定义、例子、常见误用三段展开（本地走查，非真实模型）。",
                "board": None,
            }
        elif "ONE short teaching step" in last or "lecture teacher" in last:
            payload = {
                "steps": [{
                    "title": "合成教学步骤",
                    "text": "本步骤讲解当前讲次的核心概念：先给出定义，再用一个具体例子说明它在真实系统里如何出现，"
                            "最后指出两个最常见的误用与判别方法，帮助你把概念落到可操作的判断上。"
                            "整段朗读约 40 秒，适合作为一段短步骤播放。",
                    "board": {
                        "kind": "board",
                        "title": "核心概念速览",
                        "points": ["定义：并发事务之间的可见性约定", "例子：转账场景的两个事务", "误用：把隔离级别当成锁"],
                        "code": None,
                        "formula": None,
                        "nodes": [
                            {"id": "n1", "label": "事务 A"},
                            {"id": "n2", "label": "事务 B"},
                        ],
                        "edges": [{"from_id": "n1", "to_id": "n2", "label": "冲突"}],
                        "original_refs": [],
                        "reveal_ms": [0, 4000, 8000],
                    },
                }]
            }
        elif "outline" in last or "大纲" in last:
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


class _DevSyntheticTTSAdapter:
    """仅本地走查：合成 TTS（返回 hex 音频与句级字幕）。"""

    kind = "tts"
    protocol = "minimax"

    def synthesize(self, text: str, *, voice_id=None) -> dict:
        import hashlib as _hashlib

        payload = b"ID3synthetic-audio" + text.encode("utf-8")
        subtitle = [{"word": seg, "begin_ms": i * 400, "end_ms": (i + 1) * 400}
                    for i, seg in enumerate(text.split("。") if text else []) if seg][:20]
        return {
            "model": "speech-2.8-hd",
            "voice_id": voice_id or "dev-synthetic",
            "audio_format": "mp3",
            "subtitle_enable": True,
            "subtitle_type": "sentence",
            "audio_hex": payload.hex(),
            "subtitle": subtitle,
            "text_sha256": _hashlib.sha256(text.encode()).hexdigest(),
            "vendor_trace_id": "dev-synthetic",
            "endpoint": "dev://synthetic",
        }


class _DevSyntheticOcrAdapter:
    """仅本地走查：合成 OCR 任务提交/结果（含一个结构图区域与一个文本区域）。"""

    kind = "ocr"
    protocol = "paddleocr"

    def submit(self, *, material_id: str, page_no: int, options=None) -> dict:
        return {"external_request_id": f"devjob_{material_id}_{page_no}", "status": "submitted"}

    def query(self, external_request_id: str) -> dict:
        return {"external_request_id": external_request_id, "status": "done"}

    def fetch_result(self, external_request_id: str) -> dict:
        return {
            "external_request_id": external_request_id,
            "result": {
                "layoutParsingResults": [{
                    "prunedResult": {
                        "parsing_res_list": [
                            {"block_bbox": [40, 40, 300, 120], "block_label": "text",
                             "block_content": "扫描页合成 OCR 文本 regions", "block_index": 0},
                            {"block_bbox": [40, 140, 300, 260], "block_label": "structure_diagram",
                             "block_content": "", "block_index": 1},
                        ]
                    }
                }]
            },
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
    ocr_adapter_factory: Optional[Any] = None,
    tts_adapter_factory: Optional[Any] = None,
) -> FastAPI:
    if model_adapter_factory is None or search_adapter_factory is None:
        dev_model, dev_search = _dev_synthetic_factories()
        model_adapter_factory = model_adapter_factory or dev_model
        search_adapter_factory = search_adapter_factory or dev_search
    if ocr_adapter_factory is None and os.environ.get("LEARNING_DEV_SYNTHETIC_MODEL") == "1":
        ocr_adapter_factory = lambda owner, task_id: _DevSyntheticOcrAdapter()  # noqa: E731
    if tts_adapter_factory is None and os.environ.get("LEARNING_DEV_SYNTHETIC_MODEL") == "1":
        tts_adapter_factory = lambda owner, task_id: _DevSyntheticTTSAdapter()  # noqa: E731
    storage = state.storage if state is not None else None
    holder = state.pipeline_holder if state is not None else {"pipeline": None}
    app = create_slice2_app(state=slice2_state, storage=storage, pipeline_holder=holder)
    if state is None:
        base = app.state.slice1
        from .materials.storage import MaterialStorage

        state = Slice3State(
            base.db, base.audit,
            model_adapter_factory=model_adapter_factory,
            search_adapter_factory=search_adapter_factory,
            ocr_adapter_factory=ocr_adapter_factory,
            tts_adapter_factory=tts_adapter_factory,
            storage=storage or MaterialStorage(),
            pipeline_holder=holder,
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
    from .teaching.router import create_teaching_router

    app.include_router(
        create_teaching_router(teaching=state.teaching, resolve_user=_resolve_user)
    )

    @app.exception_handler(PlanningError)
    async def planning_error(_: Request, exc: PlanningError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"error_code": exc.code, "message": exc.message}
        )

    from .teaching.errors import TeachingError

    @app.exception_handler(TeachingError)
    async def teaching_error(_: Request, exc: TeachingError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status or 400, content={"error_code": exc.code, "message": exc.message}
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

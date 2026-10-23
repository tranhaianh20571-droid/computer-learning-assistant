"""切片 3 集成应用：在切片 2 应用上挂载规划/草稿/场景/总览/即时对话路由。"""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .logging_audit.repositories import PgAuditLog, PgTaskEventStore
from .planning.chat import ChatService
from .planning.features import PlanningFeatureService
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
        self.features = PlanningFeatureService(self.planning, self.drafts)
        # 切片 4：讲次教学（步骤生成/板书/TTS/检查点/追问/恢复）
        from .teaching.service import TeachingService

        self.teaching = TeachingService(
            db, self.tasks, self.events, self.prompts, self.storage,
            model_adapter_factory=model_adapter_factory,
            tts_adapter_factory=tts_adapter_factory,
        )
        from .teaching.assessment import AssessmentService
        self.assessments = AssessmentService(self.teaching)
        from .review.service import ReviewService
        self.review = ReviewService(self.teaching)
        # 切片 5：在线清理、备份轮换与恢复锁
        from .cleanup.service import CleanupService
        from .planning.deletion import PlanningDeletionService

        self.cleanup = CleanupService(db, storage=self.storage, audit=audit)
        self.deletion = PlanningDeletionService(db, audit)
        from .memory.service import MemoryService
        self.memory = MemoryService(db, audit)
        self.chat.memory = self.memory
        self.planning.memory = self.memory
        self.teaching.memory = self.memory
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
        from .characters.distillation_models import CharacterBuildRow, CharacterBuildStageRow  # noqa: F401
        from .characters.persona import CharacterPersonaRow  # noqa: F401


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

    def stream(self, messages, **options):
        text = "事务隔离级别描述的是并发事务之间的可见性约定：读未提交最低，读已提交避免脏读，可重复读避免不可重复读，串行化最高但并发最差（合成走查回答，非真实模型）。"
        for i in range(0, len(text), 8):
            yield text[i:i + 8]
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
    protocol = "anysearch"

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


def _configured_model_descriptor(view, credentials):
    """Build connection metadata; production factories must bind task authority."""
    from .slice1.adapters import ADAPTERS
    protocol = (view["protocol"] or "").lower()
    cls = ADAPTERS.get(protocol)
    if cls is None:
        return None
    adapter = cls(endpoint=view["endpoint"], model_name=view["model_name"], credentials=credentials)
    adapter.config_id = view["config_id"]
    adapter.config_version = view["config_version"]
    adapter.http_pool_key = (adapter.config_id, adapter.config_version)
    adapter.capability_status = view["capability_status"]
    adapter.reasoning_settings = view.get("reasoning_settings", {})
    adapter.defer_capability_probe = True
    return adapter


def _config_model_factory(configs) -> Optional[Any]:
    """按用户已存能力配置构建模型适配器（设置页为真相源）。

    解析任务固定的用途连接，按协议映射适配器；
    端点由适配器统一规范化，能力测试与实际生成使用同一路径。
    """
    from .slice1.capability_refresh import CapabilityRefresher
    refresher = CapabilityRefresher(configs)

    build = _configured_model_descriptor

    def factory(owner_user_id: str, task_id: str):
        from .planning.authorization import bound_planning_config
        planning_bound, view = bound_planning_config(configs, owner_user_id, task_id)
        if not planning_bound:
            from .agents.model_routing import task_role_config
            view = task_role_config(configs, owner_user_id, task_id)
        if view is None:
            return None
        adapter = build(view, configs.resolve_credentials(view["config_id"]))
        if adapter is not None:
            adapter.task_id = task_id
            adapter.disclosure_grant_id = view.get("_planning_grant_id")
            from .agents.task_binding import bind_task_deps
            from .slice1.disclosure_service import DisclosureService
            adapter.agent_deps = bind_task_deps(configs.db, adapter, actor_id=owner_user_id,
                task_id=task_id, disclosures=DisclosureService(configs.db, PgAuditLog(configs.db)))
            refresher.register(view, owner_user_id, build)
        return adapter

    refresher.builder = build
    configs.capability_refresher = refresher
    factory.refresher = refresher
    factory.role_routing = True

    return factory


def _real_model_factory() -> Optional[Any]:
    """真实模型工厂（LEARNING_REAL_MODEL_* 环境变量）。

    协议由 LEARNING_REAL_MODEL_PROTOCOL 选择：openai（默认，/v1 兼容端点）
    或 gemini（原生 /v1beta generateContent + 原生 responseSchema；
    endpoint 由适配器统一规范化后追加 /v1beta/models/...）。
    """
    key = os.environ.get("LEARNING_REAL_MODEL_KEY")
    endpoint = os.environ.get("LEARNING_REAL_MODEL_ENDPOINT")
    if not (key and endpoint):
        return None
    model_name = os.environ.get("LEARNING_REAL_MODEL_NAME", "")
    if os.environ.get("LEARNING_REAL_MODEL_PROTOCOL", "").lower() == "gemini":
        from .slice1.adapters.gemini import GeminiAdapter

        return lambda owner, task_id: GeminiAdapter(
            endpoint=endpoint, model_name=model_name, credentials={"api_key": key}
        )
    from .slice1.adapters.openai import OpenAIAdapter

    return lambda owner, task_id: OpenAIAdapter(
        endpoint=endpoint, model_name=model_name, credentials={"api_key": key}
    )


def _real_tts_factory() -> Optional[Any]:
    """真实 TTS 工厂（LEARNING_REAL_TTS_* 环境变量；MiniMax）。"""
    key = os.environ.get("LEARNING_REAL_TTS_KEY")
    if not key:
        return None
    from .slice1.adapters.minimax import MiniMaxTTSAdapter

    endpoint = os.environ.get("LEARNING_REAL_TTS_ENDPOINT", "https://api.minimax.cn")
    model_name = os.environ.get("LEARNING_REAL_TTS_MODEL", "speech-2.8-hd")
    voice_id = os.environ.get("LEARNING_REAL_TTS_VOICE", "male-qn-qingse")
    return lambda owner, task_id: MiniMaxTTSAdapter(
        endpoint=endpoint, model_name=model_name,
        credentials={"api_key": key}, voice_id=voice_id,
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
    managed_teaching=state is None
    # 工厂优先级：显式传入 > 用户已存配置（设置页）> 真实凭据（env）> 本地合成走查；
    # 模型工厂在 slice1 状态就绪后解析（配置工厂需要 ConfigService）。
    explicit_tts_factory=tts_adapter_factory
    storage = state.storage if state is not None else None
    holder = state.pipeline_holder if state is not None else {"pipeline": None}
    app = create_slice2_app(state=slice2_state, storage=storage, pipeline_holder=holder)
    if state is None:
        base = app.state.slice1
        if explicit_tts_factory is None:
            from .teaching.tts_config import configured_tts_factory
            tts_adapter_factory=configured_tts_factory(base.configs,base.disclosures,_real_tts_factory())
        if model_adapter_factory is None:
            config_factory = _config_model_factory(base.configs)
            env_factory = _real_model_factory()
            if config_factory is not None or env_factory is not None:
                def _model_factory_chain(owner_user_id: str, task_id: str):  # noqa: E306
                    adapter = config_factory(owner_user_id, task_id) if config_factory else None
                    if adapter is None:
                        adapter = env_factory(owner_user_id, task_id) if env_factory else None
                        if adapter is not None:
                            from .agents.task_binding import bind_task_deps
                            adapter.agent_deps = bind_task_deps(base.db, adapter,
                                actor_id=owner_user_id, task_id=task_id,
                                disclosures=base.disclosures)
                    return adapter

                model_adapter_factory = _model_factory_chain
        if search_adapter_factory is None:
            from .search.service import configured_search_factory
            search_adapter_factory = configured_search_factory(base.configs)
        if model_adapter_factory is None:
            dev_model, _ = _dev_synthetic_factories()
            model_adapter_factory = dev_model
        if ocr_adapter_factory is None and os.environ.get("LEARNING_DEV_SYNTHETIC_MODEL") == "1":
            ocr_adapter_factory = lambda owner, task_id: _DevSyntheticOcrAdapter()  # noqa: E731
        if tts_adapter_factory is None and os.environ.get("LEARNING_DEV_SYNTHETIC_MODEL") == "1":
            tts_adapter_factory = lambda owner, task_id: _DevSyntheticTTSAdapter()  # noqa: E731
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
    app.state.slice1.characters.teaching = state.teaching
    state.teaching.characters = app.state.slice1.characters
    if managed_teaching:
        from .planning.authorization import PlanningAuthorization
        state.planning.authorization = PlanningAuthorization(app.state.slice1.configs,
            app.state.slice1.disclosures)
        state.teaching.disclosures = app.state.slice1.disclosures
        state.teaching.configs=app.state.slice1.configs
        state.teaching.authorization.automatic_enabled = os.environ.get("TEACHING_AUTOMATIC_AUTHORIZATION", "1") == "1"
        if state.teaching.tts_adapter_factory:
            state.teaching.tts_adapter_factory.authorize_task = state.teaching._check_authorization
        from .teaching.background import TeachingBackground
        state.teaching.background=TeachingBackground(state.teaching)
    state.features.cover_configs = app.state.slice1.configs
    state.features.cover_disclosures = app.state.slice1.disclosures
    state.features.cover_automatic_authorization = os.environ.get("COVER_AUTOMATIC_AUTHORIZATION", "1") == "1"
    if state.features.image_adapter_factory is None:
        def image_factory(owner, task_id):
            import json
            from sqlalchemy import select
            configs = app.state.slice1.configs
            from .planning.feature_models import CoverAssetRow
            with state.db.session() as sess:
                asset = sess.scalar(select(CoverAssetRow).where(CoverAssetRow.task_id == task_id))
                metadata = json.loads(asset.recipe_json or "{}") if asset else {}
            config_id = metadata.get("config_id")
            if not config_id or not asset or asset.owner_user_id != owner:
                from .slice1.errors import error
                raise error("disclosure_required", "cover authorization missing")
            view = configs.get(config_id, actor_id=owner, is_admin=False)
            if (not view.get("is_active") or view["config_version"] != metadata.get("config_version")
                    or view["kind"] != "image_generation"):
                from .slice1.errors import error
                raise error("disclosure_revoked", "cover configuration changed")
            app.state.slice1.disclosures.verify_or_audit(task_id=task_id, config_id=view["config_id"],
                content_category="generated_text", actor_id=owner, scope_snapshot=metadata.get("scope_snapshot"))
            from .slice1.adapters.imagegen import ImageGenerationAdapter
            return ImageGenerationAdapter(endpoint=view["endpoint"], model_name=view["model_name"],
                credentials=configs.resolve_credentials(view["config_id"]), max_response_bytes=16_000_000)
        state.features.image_adapter_factory = image_factory

    def _resolve_user(request: Request):
        # 登录态优先；无有效会话时以访客身份继续（auth/guest.py）
        from .auth.guest import resolve_user_or_guest

        return resolve_user_or_guest(request, app.state.slice0.auth)

    from .materials.attachments import AttachmentService, create_attachment_router
    from .materials.storage import MaterialStorage
    if app.state.slice2.materials.storage is None:
        app.state.slice2.materials.storage = state.storage
    state.attachments = AttachmentService(state.db, app.state.slice2.materials,
        app.state.slice1.configs, app.state.slice1.disclosures, state.tasks, state.storage)
    app.include_router(create_attachment_router(state.attachments, _resolve_user))
    from .materials.source_imports import SourceImports
    from .materials.source_router import create_source_router
    state.source_imports = SourceImports(state.attachments)
    app.include_router(create_source_router(state.source_imports, _resolve_user))
    from .characters.distillation import DistillationService
    from .characters.distillation_router import create_distillation_router
    state.distillation=DistillationService(app.state.slice1.characters,state.tasks,state.prompts,
        app.state.slice1.disclosures,state.planning.model_adapter_factory,state.planning.search_adapter_factory,
        media_service=app.state.slice1.character_media)
    app.include_router(create_distillation_router(state.distillation,_resolve_user))

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
            deletion=state.deletion,
            resolve_user=_resolve_user,
            features=state.features,
            teaching=state.teaching,
        )
    )
    from .planning.feature_router import create_feature_router
    app.include_router(create_feature_router(state.features, _resolve_user))

    from .teaching.router import create_teaching_router

    app.include_router(
        create_teaching_router(teaching=state.teaching, resolve_user=_resolve_user)
    )
    from .teaching.assessment_router import create_assessment_router
    app.include_router(create_assessment_router(state.assessments, _resolve_user))
    from .review.router import create_review_router
    app.include_router(create_review_router(state.review, _resolve_user))
    from .memory.router import create_memory_router
    app.include_router(create_memory_router(state.memory, _resolve_user, app.state.slice1.characters))
    from .memory.proxy import create_proxy_router
    app.include_router(create_proxy_router(state.db))

    from .cleanup.router import create_cleanup_router
    from .cleanup.manager import LocalResourceManager
    from .cleanup.resource_router import create_resource_router
    state.resources = LocalResourceManager(state)
    app.include_router(create_resource_router(state.resources, _resolve_user))

    from .lifecycle import worker_lifespan
    workers = ([state.teaching.background] if managed_teaching else []) + [
        state.features.cover_background, state.attachments, state.source_imports,
        state.distillation, state.resources]
    app.router.lifespan_context = worker_lifespan(workers, parent=app.router.lifespan_context)

    app.include_router(
        create_cleanup_router(cleanup=state.cleanup, resolve_user=_resolve_user)
    )

    @app.middleware("http")
    async def restore_lock_guard(request: Request, call_next):
        # restore_locked 期间拒绝普通用户访问（管理员路径与健康检查放行）
        path = request.url.path
        if (
            state.cleanup.is_restore_locked()
            and not path.startswith("/api/admin")
            and path != "/healthz"
        ):
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=423,
                content={"error_code": "restore_locked",
                         "message": "system locked for restore"},
            )
        return await call_next(request)

    @app.exception_handler(PlanningError)
    async def planning_error(_: Request, exc: PlanningError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"error_code": exc.code, "message": exc.message, **getattr(exc, "detail", {})}
        )

    from .teaching.errors import TeachingError
    from .cleanup.errors import CleanupError

    @app.exception_handler(CleanupError)
    async def cleanup_error(_: Request, exc: CleanupError) -> JSONResponse:
        return JSONResponse(
            status_code=getattr(exc, "status", 500),
            content={"error_code": exc.code, "message": exc.message},
        )

    from .materials.storage import StorageError

    @app.exception_handler(StorageError)
    async def resource_error(_: Request, exc: StorageError) -> JSONResponse:
        status = 403 if exc.code == "local_resources_admin_only" else 404 if exc.code == "local_resource_not_found" else 409
        return JSONResponse(status_code=status, content={"error_code": exc.code,
            "message": "本地资源操作暂未完成，请检查存储空间、权限或作业状态。"})

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

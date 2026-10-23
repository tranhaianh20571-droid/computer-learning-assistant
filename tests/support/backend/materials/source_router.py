"""Authenticated source import API; GETs never schedule external work."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
from typing import Literal

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from .errors import error
from .source_models import ImportedSourceRow
from .source_settings import ReaderConfigIn


class ImportItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    client_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    url: str | None = Field(default=None, max_length=8192)
    name: str = Field(default="", max_length=255)
    mime: str = Field(default="", max_length=120)
    chapter_urls: list[str] = Field(default_factory=list, max_length=300)
    @model_validator(mode="after")
    def source_required(self):
        if bool(self.url) == bool(self.name):
            raise ValueError("one_source_required")
        return self


class DirectoryIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    url: str = Field(min_length=1, max_length=8192)


class BatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    idempotency_key: str = Field(min_length=1, max_length=128)
    items: list[ImportItemIn] = Field(min_length=1, max_length=10)


class CommitIn(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: bool


def create_source_router(service, resolve_user):
    def no_cache(response: Response):
        response.headers["Cache-Control"] = "no-store"
    router = APIRouter(tags=["source-imports"], dependencies=[Depends(no_cache)])
    def owner(request):
        return resolve_user(request).user_id

    @router.post("/api/source-directory")
    async def directory(body: DirectoryIn, request: Request):
        actor = owner(request)
        from ..agents.contracts import AgentDeps, RunBudget
        from .web_directory import discover_directory
        def check():
            owner(request)
        deps = AgentDeps(actor_id=actor, budget=RunBudget(request_limit=1, wire_limit=6),
            check_state=check, omit_content_trace=True)
        return await discover_directory(body.url, deps)

    @router.get("/api/source-settings/{provider}")
    def settings(provider: Literal["firecrawl", "bilinote"], request: Request):
        return service.settings.get(owner(request), provider)

    @router.put("/api/source-settings/{provider}")
    def save_settings(provider: Literal["firecrawl", "bilinote"], body: ReaderConfigIn, request: Request):
        return service.settings.save(owner(request), provider, body)

    @router.post("/api/source-settings/{provider}/test")
    async def test_settings(provider: Literal["firecrawl", "bilinote"], request: Request):
        actor = owner(request)
        from ..agents.contracts import AgentDeps, RunBudget
        snapshot = await run_in_threadpool(service.settings.snapshot, actor, provider)
        audit = service.configs.audit
        audit.append("config.changed", actor_id=actor, object_type="source_profile",
            object_id=provider, result="probe_started")
        def check():
            service._connection(actor, snapshot)
        deps = AgentDeps(actor_id=actor, budget=RunBudget(request_limit=1, wire_limit=2),
            check_state=check, omit_content_trace=True)
        try:
            if provider == "firecrawl":
                from .source_clients import scrape
                credentials = service.configs.catalog_snapshot(snapshot["config_id"], actor_id=actor, is_admin=False)[1]
                await scrape(snapshot["endpoint"], credentials["api_key"], "https://example.com",
                    snapshot["options"], deps)
                result = {"connection": "available"}
            else:
                from ..agents.model_routing import role_config
                if snapshot["endpoint"] == "builtin://bilinote":
                    import os
                    import subprocess
                    import sys
                    python = os.environ.get("SOURCE_VIDEO_PYTHON", sys.executable)
                    probe = await run_in_threadpool(subprocess.run, [python, "-c",
                        "import yt_dlp, faster_whisper, imageio_ffmpeg"],
                        capture_output=True, timeout=15)
                    result = {"connection": "available" if probe.returncode == 0 else "worker_required"}
                else:
                    from .source_clients import service_json
                    key = service.configs.resolve_credentials(snapshot["config_id"])["api_key"]
                    result = await service_json(snapshot["endpoint"], "/v1/health", key, {}, deps, timeout=15)
                result["model"] = "configured" if role_config(service.configs, actor, "video_understanding") else "missing"
            audit.append("config.changed", actor_id=actor, object_type="source_profile",
                object_id=provider, result="probe_completed")
            return result
        except Exception:
            audit.append("config.changed", actor_id=actor, object_type="source_profile",
                object_id=provider, result="probe_failed")
            from ..slice1.errors import Slice1Error
            raise Slice1Error("capability_test_failed", "服务测试未完成，请检查配置或稍后重试。", 502) from None

    @router.post("/api/source-imports", status_code=201)
    def create(body: BatchIn, request: Request):
        return service.create(owner(request), body.idempotency_key, body.items)

    @router.post("/api/source-imports/{ident}/items/{item_id}/file")
    async def upload(ident: str, item_id: str, request: Request, file: UploadFile = File(...)):
        actor = owner(request)
        def stage():
            with tempfile.TemporaryDirectory(prefix="source-stage-") as directory:
                path = Path(directory) / "upload.bin"
                size = 0
                with path.open("wb") as output:
                    while chunk := file.file.read(1024 * 1024):
                        size += len(chunk)
                        if size > 512_000_000:
                            raise error("file_too_large")
                        output.write(chunk)
                service.upload(actor, ident, item_id, path)
            return service.view(actor, ident)
        return await run_in_threadpool(stage)

    @router.post("/api/source-imports/{ident}/prepare")
    def prepare(ident: str, request: Request):
        return service.prepare(owner(request), ident)

    @router.post("/api/source-imports/{ident}/commit", status_code=202)
    def commit(ident: str, body: CommitIn, request: Request):
        return service.commit(owner(request), ident, body.manifest_hash, body.confirmed)

    @router.get("/api/source-imports/{ident}")
    def get(ident: str, request: Request):
        return service.view(owner(request), ident)

    @router.post("/api/source-imports/{ident}/retry", status_code=202)
    def retry(ident: str, body: CommitIn, request: Request):
        return service.retry(owner(request), ident, body.manifest_hash, body.confirmed)

    @router.delete("/api/source-imports/{ident}")
    def remove(ident: str, request: Request):
        service.remove(owner(request), ident)
        return {"cancelled": True}

    @router.get("/api/source-materials/{material_id}")
    def source(material_id: str, request: Request):
        actor = owner(request)
        from .models import MaterialRow
        with service.db.session() as session:
            material = session.get(MaterialRow, material_id)
            row = session.get(ImportedSourceRow, material_id)
            if not material or material.deleted_at or not row or row.owner_user_id != actor:
                service.configs.audit.append("access.denied", actor_id=actor,
                    object_type="material", object_id=material_id, result="denied",
                    reason="material_not_found")
                raise error("material_not_found")
            return {"material_id": material_id, "status": material.status,
                    "result": json.loads(row.result_json), "source_version": row.source_version,
                    "failure_code": row.failure_code}

    @router.get("/api/source-materials/{material_id}/assets/{index}")
    def asset(material_id: str, index: int, request: Request):
        value = source(material_id, request)
        if value["status"] != "parsed" or not 0 <= index < len(value["result"].get("assets", [])):
            raise error("material_not_found")
        payload = service.storage.get(f"source-assets/{material_id}/{index}.jpg")
        return Response(payload, media_type="image/jpeg",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    return router

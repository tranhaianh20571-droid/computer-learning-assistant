"""Opt-in real-provider test with private inputs and an owned temporary schema."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "backend"))


def load_credentials():
    protected = ROOT / "tests" / ".secrets" / "source-imports-live.dpapi"
    script = ("$ErrorActionPreference='Stop'; $s=Get-Content -Raw -LiteralPath '" + str(protected).replace("'", "''")
        + "' | ConvertTo-SecureString; [System.Net.NetworkCredential]::new('', $s).Password")
    result = subprocess.run(["pwsh", "-NoProfile", "-EncodedCommand", base64.b64encode(script.encode("utf-16le")).decode()],
        capture_output=True, check=True, encoding="utf8")
    return json.loads(result.stdout)


def main():
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--web-only", action="store_true")
    parser.add_argument("--video-only", action="store_true")
    parser.add_argument("--model-only", action="store_true")
    parser.add_argument("--frames", type=int, default=12)
    args = parser.parse_args()
    if not args.live:
        raise SystemExit("Explicit --live is required.")
    credentials = load_credentials()
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from app.logging_audit.db import Database, get_database_url
    from app.slice3_app import Slice3State
    from app.logging_audit.repositories import PgAuditLog, PgTaskEventStore
    from app.materials.attachments import AttachmentService
    from app.materials.source_imports import SourceImports
    from app.materials.source_models import ImportedSourceRow
    from app.materials.source_router import ImportItemIn
    from app.materials.source_settings import ReaderConfigIn
    from app.materials.source_clients import classify_url
    from app.materials.storage import MaterialStorage
    from app.materials.upload import MaterialService, QuotaService
    from app.slice1.config_service import ConfigService
    from app.slice1.disclosure_service import DisclosureService
    from app.tasks.service import TaskService
    from app.agents.model_routing import ModelRoleRow
    from app.agents import native
    from app.agents.contracts import AgentDeps

    schema = "source_live_" + uuid.uuid4().hex
    output = ROOT / ".backup" / "source-imports" / schema
    output.mkdir(parents=True)
    admin = create_engine(get_database_url())
    database = None
    started = time.monotonic()
    evidence = {"schema": schema, "classification": {}, "model_calls": [], "results": {}}
    from app.materials import source_processing
    original_understand = source_processing.understand_frames
    async def traced_understand(*values, **kwargs):
        try:
            return await original_understand(*values, **kwargs)
        except Exception as failure:
            import traceback
            evidence["model_failure"] = {"type": type(failure).__name__, "frames": [
                {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                for frame in traceback.extract_tb(failure.__traceback__)]}
            raise
    source_processing.understand_frames = traced_understand
    original_transport = native.SafeModelTransport
    class RecordingTransport(original_transport):
        async def handle_async_request(self, request):
            body = await request.aread()
            data = json.loads(body) if body else {}
            if "/chat/completions" in request.url.path or request.url.path.endswith("/responses"):
                images = 0
                def count(value):
                    nonlocal images
                    if isinstance(value, dict):
                        if value.get("type") in ("image_url", "input_image"):
                            images += 1
                        for nested in value.values():
                            count(nested)
                    elif isinstance(value, list):
                        for nested in value: count(nested)
                count(data)
                evidence["model_calls"].append({"model": data.get("model"), "images": images,
                    "reasoning_effort": data.get("reasoning_effort") or data.get("reasoning", {}).get("effort")})
            return await super().handle_async_request(request)
    native.SafeModelTransport = RecordingTransport
    try:
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = make_url(get_database_url()).update_query_dict({"options": "-csearch_path=" + schema})
        database = Database(url.render_as_string(hide_password=False))
        Slice3State(database)
        database.create_all()
        owner = "source_live_" + uuid.uuid4().hex[:16]
        configs = ConfigService(database, encryption_key=uuid.uuid4().hex, audit=PgAuditLog(database))
        storage = MaterialStorage(output / "private-storage")
        attachments = AttachmentService(database, MaterialService(database, QuotaService(database), storage=storage),
            configs, DisclosureService(database, configs.audit),
            TaskService(database, PgTaskEventStore(database)), storage)
        service = SourceImports(attachments)
        # Discover the exact requested identifier without printing the catalog or credentials.
        import httpx2
        async def catalog():
            async with httpx2.AsyncClient(transport=RecordingTransport(credentials["model_endpoint"], AgentDeps()),
                    timeout=30, trust_env=False) as client:
                response = await client.get(credentials["model_endpoint"].rstrip("/") + "/models",
                    headers={"Authorization": "Bearer " + credentials["model_key"]})
                return [item["id"] for item in response.json().get("data", [])]
        models = asyncio.run(catalog())
        requested = credentials["model_name"]
        normalized = lambda value: "".join(c for c in value.lower() if c.isalnum())
        matches = [value for value in models if normalized(value) == normalized(requested)]
        if not matches:
            evidence["model_catalog"] = {"requested_available": False}
            raise RuntimeError("requested_model_unavailable")
        model_name = matches[0]
        evidence["model_catalog"] = {"requested_available": True, "selected": model_name}
        view = configs.create(actor_id=owner, is_admin=False, kind="content_model", protocol="openai",
            endpoint=credentials["model_endpoint"], model_name=model_name,
            credentials={"api_key": credentials["model_key"]},
            reasoning_settings={"mode": "effort", "effort": "low", "discovered": True, "show_summary": False})
        with database.session() as session:
            session.add(ModelRoleRow(owner_user_id=owner, role="video_understanding", config_id=view["config_id"], revision=1))
        if not args.video_only and not args.model_only:
            service.settings.save(owner, "firecrawl", ReaderConfigIn(expected_revision=0,
                endpoint="https://api.firecrawl.dev", api_key=credentials["firecrawl_key"]))
        if not args.web_only:
            service.settings.save(owner, "bilinote", ReaderConfigIn(expected_revision=0,
                endpoint="builtin://bilinote", options={"max_frames": args.frames}))
        os.environ["SOURCE_VIDEO_PYTHON"] = str(ROOT / "server" / ".venv-video" / "Scripts" / "python.exe")
        os.environ["SOURCE_ASR_MODELS_DIR"] = str(ROOT / "server" / "asr-models")
        for name in (("image",) if args.model_only else ("web", "video")):
            if args.web_only and name == "video" or args.video_only and name == "web":
                continue
            value = credentials.get(name + "_url")
            route = classify_url(value) if value else {"kind": "image", "platform": ""}
            evidence["classification"][name] = {"kind": route["kind"], "platform": route["platform"]}
            before = time.monotonic()
            item = ImportItemIn(client_id=name, url=value) if value else ImportItemIn(
                client_id=name, name="synthetic.png", mime="image/png")
            batch = service.create(owner, uuid.uuid4().hex, [item])
            if not value:
                from PIL import Image, ImageDraw, ImageFont
                image = Image.new("RGB", (800, 440), "white")
                draw = ImageDraw.Draw(image)
                font = ImageFont.truetype("arial.ttf", 52)
                draw.text((40, 40), "SOURCE TEST", fill="black", font=font)
                draw.text((40, 140), "2 + 3 = 5", fill="black", font=font)
                draw.rectangle((500, 240, 700, 400), fill="blue")
                image_path = output / "synthetic.png"
                image.save(image_path)
                service.upload(owner, batch["batch_id"], batch["items"][0]["item_id"], image_path)
            prepared = service.prepare(owner, batch["batch_id"])
            submitted = service.commit(owner, batch["batch_id"], prepared["manifest_hash"], True)
            material_id = submitted["items"][0]["material_id"]
            with database.session() as session:
                task_id = session.get(ImportedSourceRow, material_id).task_id
            service.run(task_id, owner, material_id)
            status = service.view(owner, batch["batch_id"])
            with database.session() as session:
                row = session.get(ImportedSourceRow, material_id)
                state = json.loads(row.result_json)
            (output / (name + "-private-result.json")).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf8")
            result = {"status": status["items"][0]["status"], "error_code": status["items"][0]["failure_code"],
                "seconds": round(time.monotonic() - before, 2),
                "text_chars": sum(len(block.get("content", "")) for page in state.get("page_results", []) for block in page["blocks"]),
                "sections": len(state.get("study_index", {}).get("sections", [])),
                "frames": state.get("visual_frames", 0), "unit": state.get("unit"),
                "assets": len(state.get("assets", [])), "duration": state.get("duration"),
                "frame_start_ms": min((item.get("time_ms", 0) for item in state.get("assets", [])), default=None),
                "frame_end_ms": max((item.get("time_ms", 0) for item in state.get("assets", [])), default=None),
                "transcript_kind": next((block.get("source_kind") for page in state.get("page_results", [])
                    for block in page["blocks"]), None),
                "transcript_segments": len(state.get("transcript_segments", [])),
                "has_source_jump": bool(state.get("source_url")),
                "visual_coverage_complete": state.get("visual_coverage_complete"),
                "failure_phase": state.get("failure_phase"), "failure_reason": state.get("failure_reason"),
                "failure_type": state.get("failure_type")}
            if result["status"] == "ready":
                from app.materials.corpus import search_corpus
                sample = "\n".join(block.get("content", "") for page in state.get("page_results", [])
                                  for block in page["blocks"])[:300]
                hits, _ = search_corpus(database, owner, [material_id], sample)
                result["learning_retrieval_hits"] = len(hits)
            evidence["results"][name] = result
            print(json.dumps({name: result}, ensure_ascii=False), flush=True)
    except Exception as failure:
        evidence["failure_type"] = type(failure).__name__
        import traceback
        evidence["failure_frames"] = [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
            for frame in traceback.extract_tb(failure.__traceback__)]
        diagnostic = getattr(getattr(failure, "orig", None), "diag", None)
        evidence["constraint"] = getattr(diagnostic, "constraint_name", None)
        print(json.dumps({"failure_type": type(failure).__name__}), flush=True)
    finally:
        native.SafeModelTransport = original_transport
        source_processing.understand_frames = original_understand
        evidence["total_seconds"] = round(time.monotonic() - started, 2)
        (output / "evidence.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf8")
        if database:
            database.dispose()
        if not schema.startswith("source_live_") or len(schema) != 44:
            raise RuntimeError("invalid owned schema")
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
        print("Private evidence: " + str(output))
    expected = {"image"} if args.model_only else {"web"} if args.web_only else {"video"} if args.video_only else {"web", "video"}
    if ("failure_type" in evidence or set(evidence["results"]) != expected
            or any(value["status"] != "ready" or not value.get("learning_retrieval_hits")
                for value in evidence["results"].values())):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

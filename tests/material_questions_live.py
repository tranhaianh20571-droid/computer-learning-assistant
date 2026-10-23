"""Opt-in semantic question probe on a matching, already OCRed private PDF."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--ocr", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--window-limit", type=int)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--capture-provider-error", action="store_true")
    parser.add_argument("--page", type=int, action="append",
                        help="Explicit sampled original pages; omitted means the complete document.")
    parser.add_argument("--text-only", action="store_true", help="Only the declared semantic role; no image calls.")
    args = parser.parse_args()
    folder = (ROOT / args.out).resolve()
    if not folder.is_relative_to((ROOT / ".backup").resolve()):
        parser.error("Private outputs must remain below .backup.")
    folder.mkdir(parents=True, exist_ok=True)
    source, ocr = Path(args.pdf), (ROOT / args.ocr).resolve()
    with source.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    original_run = json.loads((ocr.parent / "run.json").read_text(encoding="utf8"))
    if sha not in {d["sha256"] for d in original_run["documents"]}:
        parser.error("OCR source hash does not match the PDF.")
    progress_path = folder / "run.json"
    progress = json.loads(progress_path.read_text(encoding="utf8")) if progress_path.exists() else {}
    if progress.get("schema_cleaned"):
        raise SystemExit("Completed experiment is archived; use a new output folder for another run.")
    if progress and (progress["source_owner"] != args.owner or progress["sha256"] != sha):
        parser.error("Experiment source changed.")
    if progress and progress.get("selected_pages") != args.page:
        parser.error("Experiment page scope changed.")
    if progress and progress.get("text_only", False) != args.text_only:
        parser.error("Experiment image scope changed.")
    if progress.get("failure") and not args.retry_failed:
        parser.error("A failed provider call requires explicit --retry-failed.")
    sys.path.insert(0, str(ROOT / "server/backend"))
    from dotenv import load_dotenv
    load_dotenv(ROOT / "server/.env")
    os.environ.update(CONTENT_TRACE_ENABLED="0", OTEL_ENABLED="0", PYDANTIC_AI_NO_BANNER="1")
    from app.logging_audit.sdk_logging import install_sdk_logging_safety
    install_sdk_logging_safety()
    import httpx2
    from app.agents import native
    original_transport_init = native.SafeModelTransport.__init__
    class RecordedBytes(httpx2.AsyncByteStream):
        def __init__(self, payload):
            self.payload = payload
        async def __aiter__(self):
            yield self.payload
    class PrivateDiagnostics(httpx2.AsyncBaseTransport):
        def __init__(self, inner):
            self.inner = inner
        async def handle_async_request(self, request):
            response = await self.inner.handle_async_request(request)
            if response.status_code >= 400:
                payload = await response.aread()
                (folder / "provider-error-private.json").write_bytes(payload)
                request_data = json.loads(await request.aread())
                (folder / "provider-request-format-private.json").write_text(
                    json.dumps({"format_type": request_data.get("response_format", {}).get("type")}),
                    encoding="utf8")
                status, headers, extensions = response.status_code, response.headers, response.extensions
                headers = {k: v for k, v in headers.items() if k.lower() not in ("content-encoding", "content-length")}
                await response.aclose()
                return httpx2.Response(status, headers=headers, stream=RecordedBytes(payload), extensions=extensions)
            return response
        async def aclose(self):
            await self.inner.aclose()
    def instrument_transport(self, *values, **kwargs):
        original_transport_init(self, *values, **kwargs)
        self.inner = PrivateDiagnostics(self.inner)
    if args.capture_provider_error:
        native.SafeModelTransport.__init__ = instrument_transport
    from app.agents.model_routing import role_config
    from app.logging_audit.db import Database, get_database_url
    from app.logging_audit.repositories import PgAuditLog, PgTaskEventStore
    from app.materials.attachments import AttachmentService, validate_document
    from app.materials.models import DocumentAttachmentRow
    from app.materials.storage import MaterialStorage
    from app.materials.upload import MaterialService, QuotaService
    from app.slice1.config_service import ConfigService
    from app.slice1.disclosure_service import DisclosureService
    from app.slice3_app import Slice3State
    from app.tasks.service import TaskService
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    source_db = Database()
    original_configs = ConfigService(source_db)
    admin = create_engine(make_url(get_database_url()))
    if not progress:
        progress = {"schema": "question_quality_" + uuid.uuid4().hex,
            "source_owner": args.owner, "owner": "probe_" + uuid.uuid4().hex[:20], "sha256": sha,
            "selected_pages": args.page, "text_only": args.text_only}
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{progress["schema"]}"'))
        progress_path.write_text(json.dumps(progress, indent=2), encoding="utf8")
    schema = progress["schema"]
    if not re.fullmatch(r"question_quality_[a-f0-9]{32}", schema):
        raise SystemExit("Unexpected private schema.")
    db = Database(make_url(get_database_url()).update_query_dict(
        {"options": "-csearch_path=" + schema}).render_as_string(hide_password=False))
    Slice3State(db, PgAuditLog(db))
    db.create_all()
    configs, storage = ConfigService(db), MaterialStorage(folder / "storage")
    tasks = TaskService(db, PgTaskEventStore(db))
    service = AttachmentService(db, MaterialService(db, QuotaService(db), storage=storage),
        configs, DisclosureService(db, PgAuditLog(db)), tasks, storage)
    owner = progress["owner"]
    def save_progress():
        progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf8")
    try:
        if not progress.get("material_id"):
            parser_view = original_configs.latest_active_for_owner(args.owner, "document_parser")
            if not parser_view:
                raise RuntimeError("document_parser_missing")
            copied_parser = configs.create(actor_id=owner, is_admin=False, kind="document_parser",
                protocol=parser_view["protocol"], endpoint=parser_view["endpoint"],
                model_name=parser_view["model_name"],
                credentials=original_configs.resolve_document_credentials(parser_view["config_id"],
                    owner_user_id=args.owner, expected_version=parser_view["config_version"]))
            targets = {}
            progress["source_models"] = {}
            for role, binding_role in (("semantic", "planning"), ("visual", "video_understanding")):
                if role == "visual" and args.text_only:
                    continue
                view = role_config(original_configs, args.owner, binding_role)
                if not view:
                    continue
                copied = configs.create(actor_id=owner, is_admin=False, kind="content_model",
                    protocol=view["protocol"], endpoint=view["endpoint"], model_name=view["model_name"],
                    credentials=original_configs.resolve_credentials(view["config_id"]),
                    reasoning_settings=view.get("reasoning_settings", {}))
                targets[role] = copied
                progress["source_models"][role] = {k: view[k] for k in ("config_id", "config_version", "model_name")}
            if "semantic" not in targets:
                raise RuntimeError("semantic_model_missing")
            item = service.create(owner, validate_document("probe.pdf", None, source),
                copied_parser["config_id"], copied_parser["config_version"], uuid.uuid4().hex,
                extraction=targets)
            progress.update(material_id=item["material_id"], task_id=item["task_id"])
            with db.session() as session:
                row = session.get(DocumentAttachmentRow, item["material_id"])
                binding = json.loads(row.result_json)["extraction_binding"]
                state = json.loads(ocr.read_text(encoding="utf8"))
                if args.page:
                    from app.materials.question_extraction import add_native_pdf
                    from app.materials.study_sources import build_study_index
                    if any(not 1 <= n <= state["pages"] for n in args.page):
                        raise ValueError("invalid_sample_page")
                    add_native_pdf(state, source, lambda: None)
                    state["page_results"] = [p for p in state["page_results"] if p["page_no"] in args.page]
                    state["native_pdf_blocks"] = [b for b in state["native_pdf_blocks"] if b["page_no"] in args.page]
                    state["chunks"] = [c for c in state["chunks"] if c["page_no"] in args.page]
                    state["study_index"] = build_study_index(state)
                state["extraction_binding"] = binding
                row.result_json = json.dumps(state, ensure_ascii=False)
            save_progress()
        for saved in progress["source_models"].values():
            actual = original_configs.get(saved["config_id"], actor_id=args.owner, is_admin=False)
            if not actual["is_active"] or actual["config_version"] != saved["config_version"]:
                raise RuntimeError("source_configuration_changed")
        if args.retry_failed:
            current_task = tasks.get_task(progress["task_id"])
            if current_task["status"] in ("failed", "cancelled", "expired"):
                with db.session() as session:
                    row = session.get(DocumentAttachmentRow, progress["material_id"])
                    config_id, config_version = row.config_id, row.config_version
                retried = service.retry(progress["material_id"], owner, config_id, config_version)
                progress.setdefault("previous_task_ids", []).append(progress["task_id"])
                progress["task_id"] = retried["task_id"]
                save_progress()
        lease = tasks.claim(progress["task_id"], lease_owner="question-quality-probe",
                            lease_seconds=180, allow_reclaim_active=True)
        def guard():
            tasks.heartbeat(lease, lease_seconds=600)
            service._guard(lease, progress["material_id"], owner)
            for saved in progress["source_models"].values():
                current = original_configs.get(saved["config_id"], actor_id=args.owner, is_admin=False)
                if not current["is_active"] or current["config_version"] != saved["config_version"]:
                    raise RuntimeError("source_configuration_changed")
        last = [None]
        def checkpoint(value):
            guard()
            service._checkpoint(lease, progress["material_id"], owner, value)
            (folder / "questions-private.json").write_text(
                json.dumps(value, ensure_ascii=False, indent=2), encoding="utf8")
            bank = value.get("question_bank", {})
            status = (sum(w["status"] == "completed" for w in bank.get("windows", {}).values()),
                      bank.get("status"))
            if status != last[0]:
                print(json.dumps({"completed_windows": status[0], "status": status[1]}), flush=True)
                last[0] = status
        with db.session() as session:
            state = json.loads(session.get(DocumentAttachmentRow, progress["material_id"]).result_json)
        started = time.monotonic()
        service.questions.run(state, owner=owner, lease=lease,
            source=storage.local_path(MaterialStorage.original_key(progress["material_id"])),
            kind="pdf", guard=guard, checkpoint=checkpoint, window_limit=args.window_limit)
        checkpoint(state)
        progress["seconds_last_run"] = round(time.monotonic()-started, 2)
        progress.pop("failure", None)
        if state["question_bank"]["status"] == "completed":
            tasks.submit_result(lease, stage="parse", status="succeeded",
                publish=lambda session: service._publish(session, lease, progress["material_id"], owner, state))
            from app.materials.study_sources import (
                documents,
                freeze_sources,
                source_candidates,
            )
            with db.session() as session:
                doc = documents(session, owner, [progress["material_id"]])[0]
                refs = [doc["material_id"] + "/" + s["id"] for s in doc["index"]["sections"]
                        if not s.get("reference_only")]
                snapshot = freeze_sources(session, owner, [doc["material_id"]],
                    [("probe", {"source_section_ids": refs})])
                receipt = {}
                candidates = source_candidates(session, owner, snapshot,
                    [{"lesson_id": "probe", "goal": "复核资料题目完整性"}],
                    include_examples=True, receipt=receipt)
                (folder / "consumption-private.json").write_text(json.dumps(
                    {"snapshot": snapshot, "candidates": candidates, "receipt": receipt},
                    ensure_ascii=False, indent=2), encoding="utf8")
            progress["summary"] = state["question_bank"]["counts"]
            progress["schema_cleaned"] = True
            save_progress()
            db.dispose()
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        save_progress()
        print(json.dumps({"summary": progress.get("summary"), "seconds": progress["seconds_last_run"]}), flush=True)
    except Exception as exc:  # noqa: BLE001 - private probe captures controlled failure metadata
        import traceback
        progress["failure"] = {"type": type(exc).__name__, "code": getattr(exc, "code", "OUTPUT_INVALID")}
        progress["failure"]["frames"] = [{"file": Path(frame.filename).name,
            "line": frame.lineno, "function": frame.name} for frame in traceback.extract_tb(exc.__traceback__)]
        reasons, cursor = [], exc
        chain = []
        for _ in range(12):
            if cursor is None:
                break
            from pydantic_ai.exceptions import ModelRetry
            if isinstance(cursor, ModelRetry):
                reasons.append(str(cursor))
            chain.append({"type": type(cursor).__name__, "status": getattr(cursor, "status_code", None)})
            cursor = cursor.__cause__ or cursor.__context__
        progress["failure"]["validation_reasons"] = reasons
        progress["failure"]["cause_chain"] = chain
        save_progress()
        print(json.dumps({"status": "failed", **progress["failure"]}, ensure_ascii=False), flush=True)
        raise SystemExit(1) from None
    finally:
        db.dispose()
        source_db.dispose()
        admin.dispose()


if __name__ == "__main__":
    main()

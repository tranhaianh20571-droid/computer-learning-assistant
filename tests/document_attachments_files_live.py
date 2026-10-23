"""Explicit, resumable real-document comparison using the production attachment pipeline.

Requires --live and caller-specified inputs. Private exports and checkpoints must
stay under .backup; importing this module never reads credentials or sends files.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from threading import Thread
import time
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--files", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--config-id")
    parser.add_argument("--protocol", choices=("somark", "paddleocr_vl"), default="somark")
    parser.add_argument("--pptx-batch-limit", type=int,
                        help="Test-only lower byte limit to exercise native PPTX batches on a small deck.")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--refresh-results", action="store_true",
                        help="Re-read completed upstream jobs without resubmitting files.")
    parser.add_argument("--capture-provider-results", action="store_true",
                        help="Save private upstream JSONL responses for feature verification.")
    args = parser.parse_args()
    if args.pptx_batch_limit is not None and not 1 <= args.pptx_batch_limit <= 50_000_000:
        parser.error("--pptx-batch-limit must be between 1 and 50000000.")
    root = Path(__file__).resolve().parents[1]
    destination = (root / args.output).resolve()
    if not destination.is_relative_to((root / ".backup").resolve()):
        raise SystemExit("Private outputs must be inside .backup.")
    destination.mkdir(parents=True, exist_ok=True)
    files = [Path(p).resolve(strict=True) for p in args.files]
    sys.path.insert(0, str(root / "server" / "backend"))
    from dotenv import load_dotenv
    load_dotenv(root / "server" / ".env")
    os.environ.update(CONTENT_TRACE_ENABLED="0", OTEL_ENABLED="0", PYDANTIC_AI_NO_BANNER="1")
    from app.logging_audit.sdk_logging import install_sdk_logging_safety
    install_sdk_logging_safety()
    from sqlalchemy import create_engine, select, text
    from sqlalchemy.engine import make_url
    from app.logging_audit.db import Database, get_database_url
    from app.logging_audit.repositories import PgAuditLog, PgTaskEventStore
    from app.materials.attachments import AttachmentService, validate_document
    if args.pptx_batch_limit:
        from app.materials import attachments
        from app.materials.pptx_documents import plan_pptx_batches
        attachments.plan_pptx_batches = lambda source, **options: plan_pptx_batches(
            source, target_bytes=args.pptx_batch_limit, provider_bytes=args.pptx_batch_limit,
            guard=options.get("guard"))
    from app.materials.document_pipeline import (document_markdown, normalize_result,
        finalize_document, figure_review_pages)
    from app.materials.models import DocumentAttachmentRow
    from app.materials.storage import MaterialStorage
    from app.materials.upload import MaterialService
    from app.slice1.adapters.somark import SomarkAdapter
    from app.slice1.adapters.paddle_vl import PaddleVLAdapter
    from app.slice1.config_service import ConfigService
    from app.slice1.disclosure_service import DisclosureService
    from app.slice1.models import ServiceConfigRow, DOCUMENT_DESTINATIONS
    from app.tasks.service import TaskService
    source_db = Database()
    source_configs = ConfigService(source_db)
    checkpoint_path = destination / "run.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf8")) if checkpoint_path.exists() else None
    if checkpoint and checkpoint.get("schema_cleaned"):
        raise SystemExit("Private schema was cleaned; choose a new output folder for another live run.")
    if checkpoint and checkpoint.get("pptx_batch_limit") != args.pptx_batch_limit:
        raise SystemExit("Batch test policy changed; use a separate private output folder.")
    with source_db.session() as sess:
        candidates = list(sess.scalars(select(ServiceConfigRow).where(
            ServiceConfigRow.kind == "document_parser", ServiceConfigRow.is_active.is_(True),
            ServiceConfigRow.protocol == args.protocol)))
        if args.config_id:
            candidates = [c for c in candidates if c.config_id == args.config_id]
        if checkpoint:
            candidates = [c for c in candidates if c.config_id == checkpoint["source_config_id"]]
        if len(candidates) != 1:
            raise SystemExit("Select one configured document parser with --config-id.")
        original = candidates[0]
        source_config_id, source_owner, source_version = original.config_id, original.owner_user_id, original.config_version
    credentials = source_configs.resolve_document_credentials(source_config_id,
        owner_user_id=source_owner, expected_version=source_version)
    base_url = make_url(get_database_url())
    admin = create_engine(base_url)
    if not checkpoint:
        schema = "document_files_" + uuid.uuid4().hex
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        checkpoint = {"schema": schema, "source_config_id": source_config_id,
                      "source_version": source_version, "documents": [], "owner": "live_" + uuid.uuid4().hex[:12],
                      "pptx_batch_limit": args.pptx_batch_limit}
    schema = checkpoint["schema"]
    if not schema.startswith("document_files_") or len(schema) != 47:
        raise SystemExit("Unexpected comparison schema.")
    if checkpoint["source_version"] != source_version:
        raise SystemExit("Source configuration changed; do not silently restart paid work.")
    isolated = base_url.update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
    db = Database(isolated)
    from app.slice3_app import Slice3State
    Slice3State(db)
    db.create_all()
    audit = PgAuditLog(db)
    configs = ConfigService(db, audit=audit)
    if "config_id" not in checkpoint:
        endpoint, model = DOCUMENT_DESTINATIONS[args.protocol]
        config = configs.create(actor_id=checkpoint["owner"], is_admin=False,
            kind="document_parser", protocol=args.protocol, endpoint=endpoint,
            model_name=model, credentials=credentials)
        checkpoint["config_id"] = config["config_id"]
    credentials = {}

    adapter_type = PaddleVLAdapter if args.protocol == "paddleocr_vl" else SomarkAdapter
    class PinnedAdapter(adapter_type):
        def _source_guard(self):
            current = source_configs.get(source_config_id, actor_id=source_owner, is_admin=False)
            if not current["is_active"] or current["config_version"] != source_version:
                from app.slice1.errors import AdapterError
                raise AdapterError("credential_invalid", status=409)
        def _post(self, *values, **options):
            self._source_guard()
            return super()._post(*values, **options)
        def _job(self, method, suffix="", **options):
            data = super()._job(method, suffix, **options)
            if args.capture_provider_results:
                if method == "POST" and data.get("jobId"):
                    (destination / "provider-job-private.json").write_text(
                        json.dumps({"job_id": data["jobId"]}), encoding="utf8")
                if method == "GET" and data.get("state") == "failed":
                    (destination / "provider-failure-private.json").write_text(
                        json.dumps(data, ensure_ascii=False), encoding="utf8")
            return data
        def _wire(self, *values, **options):
            self._source_guard()
            raw = super()._wire(*values, **options)
            if (args.capture_provider_results and options.get("authenticated") is False and raw
                    and raw.lstrip().startswith(b"{")):
                import hashlib
                (destination / ("provider-result-" + hashlib.sha256(raw).hexdigest()[:16] + ".jsonl")).write_bytes(raw)
            return raw

    storage = MaterialStorage(destination / "private-storage")
    materials = MaterialService(db, storage=storage)
    tasks = TaskService(db, PgTaskEventStore(db))
    service = AttachmentService(db, materials, configs, DisclosureService(db, audit), tasks, storage,
        adapter_factory=lambda view, key: PinnedAdapter(endpoint=view["endpoint"],
            credentials=key, max_response_bytes=32_000_000))

    def save():
        checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf8")

    save()
    success = True
    try:
        for number, path in enumerate(files, 1):
            validated = validate_document(path.name, None, path)
            old = next((d for d in checkpoint["documents"] if d["number"] == number), None)
            if old and old["sha256"] != validated.sha256:
                raise SystemExit("Input changed; choose a different private output folder.")
            if not old:
                item = service.create(checkpoint["owner"], validated, checkpoint["config_id"], 1,
                                      "live-file-" + str(number))
                old = {"number": number, "material_id": item["material_id"],
                       "sha256": validated.sha256, "seconds": 0}
                checkpoint["documents"].append(old)
                save()
            view = service.view(old["material_id"], checkpoint["owner"])
            if view["status"] == "failed" and args.retry_failed:
                view = service.retry(old["material_id"], checkpoint["owner"], checkpoint["config_id"], 1)
            failures = []
            def execute():
                try:
                    service.run(view["task_id"])
                except Exception as exc:
                    failures.append(type(exc).__name__)
            started = time.monotonic()
            if view["status"] == "parsing":
                worker = Thread(target=execute, daemon=True)
                worker.start()
                while worker.is_alive():
                    worker.join(timeout=10)
                    progress = service.view(old["material_id"], checkpoint["owner"])
                    print(json.dumps({"document": number, "status": progress["status"],
                        "pages": progress["page_count"], "processed": progress["processed_pages"],
                        "batches": progress["completed_batches"]}), flush=True)
                old["seconds"] += round(time.monotonic() - started, 2)
            view = service.view(old["material_id"], checkpoint["owner"])
            with db.session() as sess:
                state = json.loads(sess.get(DocumentAttachmentRow, old["material_id"]).result_json)
            if args.refresh_results and view["status"] == "parsed":
                adapter = PinnedAdapter(endpoint=DOCUMENT_DESTINATIONS[args.protocol][0],
                    credentials=configs.resolve_document_credentials(
                    checkpoint["config_id"], owner_user_id=checkpoint["owner"], expected_version=1),
                    max_response_bytes=32_000_000)
                refreshed = dict(state, chunks=[], page_results=[], outline=[])
                if args.protocol == "paddleocr_vl":
                    from app.slice1.adapters.paddle_vl import LEGACY_DOCUMENT_OPTIONS
                    adapter.document_options = state.get("ocr_options", dict(LEGACY_DOCUMENT_OPTIONS))
                try:
                    for batch in state["batches"]:
                        output = adapter.check_document(batch["job_id"])
                        if output.get("status") != "SUCCESS":
                            raise RuntimeError("Completed upstream result is no longer available.")
                        normalized = normalize_result(
                            output,
                            expected_pages=batch["end"] - batch["start"] + 1,
                            page_offset=batch["start"] - 1,
                            image_understanding_enabled=True,
                        )
                        for field in ("chunks", "page_results", "outline"):
                            refreshed[field].extend(normalized[field])
                    state = finalize_document(refreshed)
                    with db.session() as sess:
                        row = sess.get(DocumentAttachmentRow, old["material_id"])
                        row.result_json = json.dumps(state, ensure_ascii=False)
                    view = service.view(old["material_id"], checkpoint["owner"])
                finally:
                    adapter.close()
            covered = {p["page_no"] for p in state.get("page_results", [])}
            missing = sorted(set(range(1, (validated.page_count or 0) + 1)) - covered)
            total_chars = sum(len(c["text"]) for c in state.get("chunks", []))
            blocks = [b for p in state.get("page_results", []) for b in p["blocks"]]
            metrics = {"number": number, "input_pages": validated.page_count, "bytes": validated.size_bytes,
                "unit": view.get("unit", "page"),
                "status": view["status"], "strategy": view["strategy"], "processed_pages": len(covered),
                "missing_pages": missing, "empty_pages": view["empty_pages"],
                "complex_table_pages": view["complex_table_pages"], "seconds": old["seconds"],
                "figure_review_pages": figure_review_pages(state),
                "figure_understanding_pages": sorted({
                    page["page_no"] for page in state.get("page_results", [])
                    if any(block.get("image_understanding")
                           for block in page.get("blocks", []))
                }),
                "batches": view["batches"], "chars": total_chars, "failure_code": view["failure_code"],
                "tables": sum(b["type"] == "table" for b in blocks),
                "formulas": sum(b["type"] == "formula" or b["format"] == "latex" for b in blocks),
                "headings": view["outline_count"], "worker_errors": failures}
            old["metrics"] = metrics
            state_path = destination / f"document-{number}-private.json"
            state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf8")
            report = [f"# {path.stem}：识别结果\n",
                "| 项目 | 本次结果 |\n| --- | --- |",
                f"| 输入页数 | {validated.page_count} |",
                f"| 输入大小 | {validated.size_bytes:,} 字节 |",
                f"| 处理策略 | {view['strategy']} |",
                f"| 识别状态 | {view['status']} |",
                f"| 已返回页数 | {len(covered)} / {validated.page_count} |",
                f"| 累计运行耗时 | {old['seconds']:.2f} 秒（含提交、轮询、拆分及合并，不含两次运行间隔） |",
                f"| 正文字符 | {total_chars:,} |",
                f"| 表格 / 公式块 / 标题 | {metrics['tables']} / {metrics['formulas']} / {metrics['headings']} |",
                f"| 失败码 | {view['failure_code'] or '无'} |",
                f"| 未返回页 | {', '.join(map(str,missing)) or '无'} |",
                f"| 未返回文字的页 | {', '.join(map(str,view['empty_pages'])) or '无'} |",
                f"| 复杂表格待核对页 | {', '.join(map(str,view['complex_table_pages'])) or '无'} |",
                f"| 图片未返回图中文字的页 | {', '.join(map(str,metrics['figure_review_pages'])) or '无'} |",
                f"| 已返回图像理解的页 | {', '.join(map(str,metrics['figure_understanding_pages'])) or '无'} |",
                "\n页数完整只表示每页有返回记录，不代表逐字、公式和知识点均经人工校对。\n",
                "## 批次记录\n\n| 原文件页范围 | 状态 |\n| --- | --- |"]
            for batch in view["batches"]:
                report.append(f"| {batch['start_page']}-{batch['end_page']} | {batch['status']} |")
            if state.get("outline"):
                report.append("\n## 从原文标题提取的结构索引\n")
                for heading in state["outline"]:
                    title = heading["title"].replace("\n", " ")
                    report.append(f"- 第 {heading['page_no']} 页：{title}")
            report.append("\n---\n\n# 逐页识别原文\n")
            report.append(service.markdown(old["material_id"], checkpoint["owner"]) if view["status"] == "parsed"
                          else document_markdown(state))
            (destination / f"document-{number}.md").write_text("\n".join(report), encoding="utf8")
            save()
            print(json.dumps(metrics, ensure_ascii=False), flush=True)
            if view["status"] != "parsed" or failures:
                success = False
                # Credential/quota denial must stop further paid submissions.
                if view["failure_code"] in ("MODEL_CREDENTIAL_INVALID", "UPLOAD_QUOTA_EXCEEDED"):
                    break
        return 0 if success else 1
    finally:
        db.dispose()
        source_db.dispose()
        admin.dispose()


if __name__ == "__main__":
    sys.exit(main())

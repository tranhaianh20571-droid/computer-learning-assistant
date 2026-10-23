"""Explicit real-provider smoke test in a disposable schema; never imported as a test."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid
import zipfile


def main():
    if sys.argv[1:] != ["--live"]:
        raise SystemExit("Use --live explicitly; the key is read from standard input.")
    key = sys.stdin.readline().strip()
    if not key:
        raise SystemExit("Missing credential on standard input.")
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "server" / "backend"))
    from app.logging_audit.sdk_logging import install_sdk_logging_safety
    install_sdk_logging_safety()
    from app.logging_audit.db import Database, get_database_url
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    base_url = make_url(get_database_url())
    schema = "document_live_" + uuid.uuid4().hex
    admin = create_engine(base_url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    url = base_url.update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
    db = Database(url)
    try:
        from app.slice3_app import Slice3State
        from app.logging_audit.repositories import PgAuditLog, PgTaskEventStore
        from app.materials.attachments import AttachmentService, validate_document
        from app.materials.storage import MaterialStorage
        from app.materials.upload import MaterialService
        from app.planning.retrieval import PlanningRetrievalService
        from app.slice1.config_service import ConfigService
        from app.slice1.disclosure_service import DisclosureService
        from app.slice1.models import SOMARK_ENDPOINT
        from app.tasks.service import TaskService
        Slice3State(db)
        db.create_all()
        owner = "synthetic_document_live"
        audit = PgAuditLog(db)
        configs = ConfigService(db, encryption_key=uuid.uuid4().hex, audit=audit)
        config = configs.create(actor_id=owner, is_admin=False, kind="document_parser", protocol="somark",
            endpoint=SOMARK_ENDPOINT, model_name="", credentials={"api_key": key})
        key = ""
        from reportlab.pdfgen import canvas
        pdf_bytes = io.BytesIO()
        canvas_doc = canvas.Canvas(pdf_bytes)
        canvas_doc.drawString(50, 700, "Synthetic document validation")
        canvas_doc.drawString(50, 660, "Database transactions preserve atomicity. Marker 24681357.")
        canvas_doc.showPage()
        canvas_doc.save()
        word_bytes = io.BytesIO()
        with zipfile.ZipFile(word_bytes, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml",
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
            archive.writestr("_rels/.rels",
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
            archive.writestr("word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                '<w:p><w:r><w:t>合成文档识别测试</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>数据库事务具有原子性、一致性、隔离性和持久性。校验编号：13572468。</w:t></w:r></w:p>'
                '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>')
        metrics = []
        with tempfile.TemporaryDirectory(prefix="document-live-") as directory:
            storage = MaterialStorage(Path(directory))
            materials = MaterialService(db, storage=storage)
            tasks = TaskService(db, PgTaskEventStore(db))
            service = AttachmentService(db, materials, configs, DisclosureService(db, audit), tasks, storage)
            for kind, payload, marker in (("pdf", pdf_bytes.getvalue(), "24681357"),
                                           ("docx", word_bytes.getvalue(), "13572468")):
                start = time.perf_counter()
                validated = validate_document("synthetic." + kind, None, payload)
                item = service.create(owner, validated, config["config_id"], 1, "synthetic-live-" + kind)
                service.run(item["task_id"])
                view = service.view(item["material_id"], owner)
                retrieval = PlanningRetrievalService(db).assemble(owner_user_id=owner,
                    material_ids=[item["material_id"]], query="数据库事务")
                success = view["status"] == "parsed" and any(marker in e["text"] for e in retrieval.excerpts)
                metrics.append({"kind": kind, "success": success, "status": view["status"],
                    "failure_code": view["failure_code"], "pages": view["page_count"],
                    "seconds": round(time.perf_counter() - start, 2), "retrieval_marker_found": success,
                    "library_items": len(materials.list_for_owner(owner))})
                service.remove(item["material_id"], owner)
        destination = root / ".backup" / "document-attachments"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "live-summary.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(metrics, ensure_ascii=False))
        return 0 if all(item["success"] for item in metrics) else 1
    finally:
        db.dispose()
        if not schema.startswith("document_live_") or len(schema) != 46:
            raise RuntimeError("Unexpected disposable schema identity")
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


if __name__ == "__main__":
    sys.exit(main())

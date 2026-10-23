"""Synthetic Somark wire, personal configuration, durable attachment and retrieval contracts."""
import io
import json
import uuid
import zipfile

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from types import SimpleNamespace

from backend.logging_audit.db import Database
from backend.logging_audit.repositories import PgAuditLog, PgTaskEventStore
from backend.materials.attachments import (AttachmentService, create_attachment_router,
                                           normalize_result, validate_document)
from backend.materials.errors import MaterialsError
from backend.materials.models import DocumentAttachmentRow, MaterialRow
from backend.materials.router import install_materials_error_handlers
from backend.materials.storage import MaterialStorage
from backend.materials.upload import MaterialService, QuotaService
from backend.planning.retrieval import PlanningRetrievalService
from backend.slice1.adapters.somark import SomarkAdapter
from backend.slice1.config_service import ConfigService
from backend.slice1.disclosure_service import DisclosureService
from backend.slice1.errors import AdapterError, Slice1Error
from backend.slice1.models import SOMARK_ENDPOINT
from backend.tasks.service import TaskService


def pdf(pages=1):
    from reportlab.pdfgen import canvas
    output = io.BytesIO()
    doc = canvas.Canvas(output)
    for page in range(pages):
        doc.drawString(30, 700, f"Synthetic database attachment {page + 1}")
        doc.showPage()
    doc.save()
    return output.getvalue()


def docx(*, macro=False):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>')
        if macro:
            archive.writestr("word/vbaProject.bin", b"synthetic")
    return output.getvalue()


def result():
    return {"status": "SUCCESS", "result": {"outputs": {"json": {"pages": [
        {"page_num": 0, "blocks": [
            {"type": "header", "content": "synthetic watermark"},
            {"type": "title", "content": "合成数据库教材"},
            {"type": "text", "content": "数据库事务具有原子性、一致性、隔离性和持久性。"},
            {"type": "table", "format": "html", "content": "<table><tr><td>隔离级别</td><td>可重复读</td></tr></table>"},
            {"type": "figure", "content": "synthetic hallucinated description",
             "image_ocr": [{"text": "提交事务"}, {"text": "回滚事务"}]},
        ]}]}}}}


class Adapter:
    def __init__(self):
        self.calls = []
        self.on_check = None
        self.fail = None
    def submit_document(self, payload, kind):
        self.calls.append((kind, len(payload)))
        if self.fail:
            raise self.fail
        return "synthetic-job"
    def check_document(self, ident):
        if self.on_check:
            self.on_check()
        return result()
    def close(self):
        pass


@pytest.fixture
def env(tmp_path):
    db = Database()
    db.create_all()
    owner = "synthetic_" + uuid.uuid4().hex[:12]
    audit = PgAuditLog(db)
    configs = ConfigService(db, encryption_key="synthetic-encryption-key-for-documents", audit=audit)
    config = configs.create(actor_id=owner, is_admin=False, kind="document_parser", protocol="somark",
        endpoint=SOMARK_ENDPOINT, model_name="", credentials={"api_key": "synthetic-document-key"})
    storage = MaterialStorage(tmp_path)
    materials = MaterialService(db, QuotaService(db), storage=storage)
    tasks = TaskService(db, PgTaskEventStore(db))
    adapter = Adapter()
    service = AttachmentService(db, materials, configs, DisclosureService(db, audit), tasks, storage,
        adapter_factory=lambda *_: adapter)
    yield SimpleNamespace(**locals())
    db.dispose()


def create(env, key=None):
    return env.service.create(env.owner, validate_document("synthetic.pdf", "application/pdf", pdf()),
        env.config["config_id"], env.config["config_version"], key or uuid.uuid4().hex)


def test_realistic_pdf_and_docx_signatures():
    assert validate_document("a.PDF", "application/pdf", pdf()).page_count == 1
    assert validate_document("a.docx", None, docx()).kind == "docx"


@pytest.mark.parametrize("name,mime,payload", [
    ("a.pdf", "application/pdf", b"not-a-pdf"), ("a.doc", None, b"not-ole"),
    ("a.docx", None, b"PK\0\0"), ("a.docx", None, docx(macro=True)),
    ("a.exe", None, b"synthetic"), ("a.pdf", "text/plain", b"%PDF-synthetic"),
])
def test_invalid_documents_rejected(name, mime, payload):
    with pytest.raises(MaterialsError):
        validate_document(name, mime, payload)


def test_pdf_page_and_size_limits(monkeypatch):
    from backend.materials import attachments
    assert validate_document("synthetic.pdf", None, pdf(301)).page_count == 301
    monkeypatch.setattr(attachments, "pdf_page_count", lambda _: 2001)
    with pytest.raises(MaterialsError, match="too_many_pages"):
        validate_document("a.pdf", None, b"%PDF-synthetic")
    monkeypatch.setattr(attachments, "MAX_DOCUMENT_BYTES", 5)
    with pytest.raises(MaterialsError, match="file_too_large"):
        validate_document("a.pdf", None, b"%PDF-synthetic")


def test_normalization_preserves_page_header_and_table_text_excludes_generated_description():
    normalized = normalize_result(result())
    text = json.dumps(normalized, ensure_ascii=False)
    assert "可重复读" in text and "提交事务" in text
    assert "hallucinated" not in text and "watermark" in text
    assert normalized["chunks"][0]["page_no"] == 1
    assert any(block["type"] == "table" and block["format"] == "html"
               for block in normalized["page_results"][0]["blocks"])


def test_image_understanding_is_separate_from_ocr_and_marked_as_untrusted():
    from backend.materials.document_pipeline import document_markdown, figure_understanding_pages
    data = {"result": {"outputs": {"json": {"pages": [{"page_num": 0, "blocks": [
        {"type": "figure", "content": "流程图显示事务提交后进入持久化阶段。",
         "image_ocr": [{"text": "提交"}, {"text": "持久化"}]}]}]}}}}
    normalized = normalize_result(data, expected_pages=1, image_understanding_enabled=True)
    figure = normalized["page_results"][0]["blocks"][0]
    assert figure["content"] == "提交\n持久化"
    assert figure["image_understanding"] == "流程图显示事务提交后进入持久化阶段。"
    assert figure_understanding_pages(normalized) == [1]
    assert any(chunk["kind"] == "image_understanding" for chunk in normalized["chunks"])
    output = document_markdown(normalized)
    assert "非逐字 OCR" in output and "流程图显示事务提交后进入持久化阶段" in output


def test_image_understanding_does_not_consume_figure_content_by_default():
    data = {"result": {"outputs": {"json": {"pages": [{"page_num": 0, "blocks": [
        {"type": "figure", "content": "synthetic description"}]}]}}}}
    normalized = normalize_result(data, expected_pages=1)
    assert normalized["page_results"][0]["blocks"][0]["content"] == ""
    assert "image_understanding" not in normalized["page_results"][0]["blocks"][0]


@pytest.mark.parametrize("bad", [{}, {"result": {"outputs": {"json": {"pages": []}}}},
    {"result": {"outputs": {"json": {"pages": [{"page_num": -1, "blocks": []}]}}}}])
def test_malformed_results_rejected(bad):
    with pytest.raises(ValueError):
        normalize_result(bad)


def test_personal_config_is_encrypted_private_and_destination_fixed(env):
    public = env.configs.list(actor_id=env.owner, is_admin=False)
    assert env.config["config_id"] in [c["config_id"] for c in public]
    assert "synthetic-document-key" not in json.dumps(public)
    assert env.configs.list(actor_id="synthetic-other", is_admin=True) == []
    with pytest.raises(Slice1Error):
        env.configs.update(env.config["config_id"], actor_id="synthetic-other", is_admin=True,
            credentials={"api_key": "synthetic-other-key"})
    with pytest.raises(Slice1Error):
        env.configs.update(env.config["config_id"], actor_id=env.owner, is_admin=False,
            endpoint="https://synthetic-other.example")
    with pytest.raises(Slice1Error):
        env.configs.create(actor_id=env.owner, is_admin=False, kind="ocr", protocol="paddleocr",
            endpoint="https://synthetic.example", model_name="", credentials={"api_key": "synthetic"})


def test_attachment_end_to_end_and_library_isolation(env):
    item = create(env)
    assert env.materials.list_for_owner(env.owner) == []
    env.service.run(item["task_id"])
    view = env.service.view(item["material_id"], env.owner)
    assert view["status"] == "parsed" and view["figure_understanding_pages"] == [1]
    retrieval = PlanningRetrievalService(env.db).assemble(owner_user_id=env.owner,
        material_ids=[item["material_id"]], query="数据库事务")
    assert retrieval.excerpts and any("原子性" in e["text"] for e in retrieval.excerpts)
    assert all(ref["page_no"] == 1 for ref in retrieval.refs)
    assert not PlanningRetrievalService(env.db).assemble(owner_user_id="synthetic-other",
        material_ids=[item["material_id"]], query="数据库").excerpts
    assert env.tasks.get_task(item["task_id"])["status"] == "succeeded"
    assert len(env.adapter.calls) == 1


def test_existing_upstream_job_is_polled_without_resubmitting(env):
    item = create(env)
    with env.db.session() as sess:
        sess.get(DocumentAttachmentRow, item["material_id"]).external_job_id = "synthetic-existing-job"
    env.service.run(item["task_id"])
    assert env.adapter.calls == []
    assert env.service.view(item["material_id"], env.owner)["status"] == "parsed"


def test_unknown_submit_outcome_requires_explicit_retry(env):
    item = create(env)
    with env.db.session() as sess:
        sess.get(DocumentAttachmentRow, item["material_id"]).external_job_id = "submitting"
    env.service.run(item["task_id"])
    assert env.service.view(item["material_id"], env.owner)["status"] == "failed"
    assert env.adapter.calls == []
    retry = env.service.retry(item["material_id"], env.owner, env.config["config_id"], 1)
    env.service.run(retry["task_id"])
    assert env.service.view(item["material_id"], env.owner)["status"] == "parsed"
    assert len(env.adapter.calls) == 1


def test_changed_configuration_blocks_outbound(env):
    item = create(env)
    env.configs.update(env.config["config_id"], actor_id=env.owner, is_admin=False,
        credentials={"api_key": "synthetic-new-key"})
    env.service.run(item["task_id"])
    assert env.adapter.calls == []
    assert env.service.view(item["material_id"], env.owner)["status"] == "failed"


def test_storage_failure_rolls_back_material_and_reservation(env, monkeypatch):
    before = env.materials.quotas.snapshot(env.owner)["reserved_bytes"]
    def failed(*_):
        raise RuntimeError("synthetic-storage-failure")
    monkeypatch.setattr(env.storage, "put", failed)
    with pytest.raises(RuntimeError):
        create(env)
    assert env.service.list(env.owner) == []
    assert env.materials.quotas.snapshot(env.owner)["reserved_bytes"] == before


def test_unparsed_attachment_cannot_be_submitted_to_planning(env):
    from backend.planning.retrieval import validate_material_ownership
    from backend.planning.errors import PlanningError
    item = create(env)
    with pytest.raises(PlanningError):
        validate_material_ownership(env.db, env.owner, [item["material_id"]])


def test_rate_limited_poll_preserves_job_for_retry(env):
    item = create(env)
    def limited(_):
        raise AdapterError("rate_limited", status=429)
    env.adapter.check_document = limited
    env.service.run(item["task_id"])
    assert env.service.view(item["material_id"], env.owner)["failure_code"] == "MODEL_RATE_LIMITED"
    with env.db.session() as sess:
        assert sess.get(DocumentAttachmentRow, item["material_id"]).external_job_id == "synthetic-job"


def test_revoked_grant_blocks_publish(env):
    item = create(env)
    env.adapter.on_check = lambda: env.service.disclosures.revoke_for_config(env.config["config_id"], reason="synthetic")
    env.service.run(item["task_id"])
    assert env.service.view(item["material_id"], env.owner)["status"] == "failed"
    assert env.service.view(item["material_id"], env.owner)["failure_code"] == "MODEL_CREDENTIAL_INVALID"
    with env.db.session() as sess:
        state = json.loads(sess.get(DocumentAttachmentRow, item["material_id"]).result_json)
        assert not state["chunks"] and not state["page_results"]


def test_delete_while_provider_responds_does_not_resurrect(env):
    item = create(env)
    env.adapter.on_check = lambda: env.service.remove(item["material_id"], env.owner)
    env.service.run(item["task_id"])
    with pytest.raises(MaterialsError):
        env.service.view(item["material_id"], env.owner)
    with env.db.session() as sess:
        assert sess.get(DocumentAttachmentRow, item["material_id"]).result_json == "{}"
        assert sess.get(MaterialRow, item["material_id"]).deleted_at is not None


def test_delete_clears_text_and_other_owner_cannot_read_or_delete(env):
    item = create(env)
    env.service.run(item["task_id"])
    with pytest.raises(MaterialsError):
        env.service.remove(item["material_id"], "synthetic-other")
    env.service.remove(item["material_id"], env.owner)
    with env.db.session() as sess:
        assert sess.get(DocumentAttachmentRow, item["material_id"]).result_json == "{}"
    assert not PlanningRetrievalService(env.db).assemble(owner_user_id=env.owner,
        material_ids=[item["material_id"]], query="数据库").excerpts


def test_idempotent_upload_never_deletes_original_on_conflict(env):
    validated = validate_document("synthetic.docx", None, docx())
    first = env.service.create(env.owner, validated, env.config["config_id"], 1, "synthetic-idem")
    second = env.service.create(env.owner, validated, env.config["config_id"], 1, "synthetic-idem")
    assert first["material_id"] == second["material_id"]
    with pytest.raises(MaterialsError):
        env.service.create(env.owner, validate_document("a.pdf", None, pdf()), env.config["config_id"], 1, "synthetic-idem")
    assert env.service.view(first["material_id"], env.owner)["status"] == "parsing"


@pytest.mark.parametrize("code,error_code", [(1107, "credential_invalid"), (1123, "quota_exceeded"),
                                            (1124, "rate_limited"), (1102, "timeout"), (1137, "response_invalid")])
def test_provider_errors_sanitized(code, error_code):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200,
        json={"code": code, "message": "synthetic-private-body"})))
    adapter = SomarkAdapter(endpoint=SOMARK_ENDPOINT, credentials={"api_key": "synthetic-secret"}, client=client)
    with pytest.raises(AdapterError) as caught:
        adapter._post("/usage")
    assert caught.value.code == error_code
    assert "synthetic-private" not in str(caught.value)
    client.close()


def test_wire_sends_multipart_key_generic_filename_and_preserves_image_and_page_context():
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"task_id": "synthetic-job"}})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    adapter = SomarkAdapter(endpoint=SOMARK_ENDPOINT, credentials={"api_key": "synthetic-secret"}, client=client)
    assert adapter.submit_document(b"%PDF-synthetic", "pdf") == "synthetic-job"
    body = seen[0].content.decode()
    assert 'filename="document.pdf"' in body and "synthetic-secret" in body
    assert '"enable_image_understanding": true' in body
    assert '"keep_header_footer": true' in body
    assert "authorization" not in seen[0].headers
    client.close()


def test_confirmation_and_http_ownership_guard(env):
    app = FastAPI()
    app.include_router(create_attachment_router(env.service, lambda _: SimpleNamespace(user_id=env.owner)))
    install_materials_error_handlers(app)
    from fastapi.responses import JSONResponse
    @app.exception_handler(Slice1Error)
    async def controlled(_, exc):
        return JSONResponse({"error_code": exc.code}, status_code=exc.status)
    with TestClient(app) as client:
        response = client.post("/api/attachments", files={"file": ("a.pdf", pdf(), "application/pdf")},
            data={"config_id": env.config["config_id"], "config_version": "1", "idempotency_key": "synthetic"})
        assert response.status_code == 409
        assert env.adapter.calls == []
        assert client.get("/api/attachments/synthetic-missing").status_code == 404


def test_preflight_counts_locally_and_download_is_private(env):
    app = FastAPI()
    app.include_router(create_attachment_router(env.service, lambda _: SimpleNamespace(user_id=env.owner)))
    install_materials_error_handlers(app)
    with TestClient(app) as client:
        preflight = client.post("/api/attachments/preflight",
            files={"file": ("synthetic.pdf", pdf(12), "application/pdf")})
        assert preflight.status_code == 200
        assert preflight.json()["page_count"] == 12
        assert preflight.json()["strategy"] == "direct"
        assert env.adapter.calls == [] and env.service.list(env.owner) == []
        uploaded = client.post("/api/attachments",
            files={"file": ("synthetic.pdf", pdf(), "application/pdf")},
            data={"confirmed": True, "config_id": env.config["config_id"],
                  "config_version": 1, "idempotency_key": "synthetic-streamed"}).json()
        ident = uploaded["material_id"]
        assert client.get(f"/api/attachments/{ident}/markdown").status_code == 409
        env.service.run(uploaded["task_id"])
        response = client.get(f"/api/attachments/{ident}/markdown")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "<table>" in response.text and "原文件第 1 页" in response.text
        assert "图像理解（非逐字 OCR，仅供辅助理解）" in response.text
        with pytest.raises(MaterialsError):
            env.service.markdown(ident, "synthetic-other")
        env.service.remove(ident, env.owner)
        assert client.get(f"/api/attachments/{ident}/markdown").status_code == 404


@pytest.mark.parametrize("pages,strategy,batches", [(3, "short", 1), (12, "direct", 1), (51, "batched", 2)])
def test_batch_planning_uses_actual_pdf_pages(pages, strategy, batches):
    from backend.materials.document_pipeline import plan_batches, pdf_batch_bytes
    import pypdfium2 as pdfium
    payload = pdf(pages)
    state = plan_batches(payload, kind="pdf", pages=pages, size=len(payload))
    assert state["strategy"] == strategy and len(state["batches"]) == batches
    covered = []
    for batch in state["batches"]:
        covered.extend(range(batch["start"], batch["end"] + 1))
        if strategy == "batched":
            with pdfium.PdfDocument(pdf_batch_bytes(payload, batch["start"], batch["end"])) as part:
                assert len(part) == batch["end"] - batch["start"] + 1
    assert covered == list(range(1, pages + 1))


def test_batch_bytes_limit_is_checked_after_serialization(monkeypatch):
    from backend.materials import document_pipeline as pipeline
    payload = pdf(4)
    single = len(pipeline.pdf_batch_bytes(payload, 1, 1))
    monkeypatch.setattr(pipeline, "BATCH_TARGET_BYTES", single + 100)
    state = pipeline.plan_batches(payload, kind="pdf", pages=4, size=len(payload))
    assert len(state["batches"]) == 4
    assert all(b["size_bytes"] <= pipeline.BATCH_TARGET_BYTES for b in state["batches"])
    monkeypatch.setattr(pipeline, "PROVIDER_MAX_BYTES", 100)
    with pytest.raises(ValueError, match="single_page_too_large"):
        pipeline.plan_batches(payload, kind="pdf", pages=4, size=len(payload))


def test_missing_duplicate_and_out_of_range_provider_pages_are_not_complete():
    for numbers in ([0, 2], [0, 0], [1, 2]):
        data = {"result": {"outputs": {"json": {"pages": [
            {"page_num": n, "blocks": [{"type": "text", "content": "synthetic"}]} for n in numbers]}}}}
        with pytest.raises(ValueError):
            normalize_result(data, expected_pages=2)


def test_empty_page_has_explicit_record_and_formula_table_structure_survives():
    from backend.materials.document_pipeline import document_markdown
    data = {"result": {"outputs": {"json": {"pages": [
        {"page_num": 1, "blocks": []},
        {"page_num": 0, "blocks": [
            {"type": "formula", "format": "latex", "content": r"P(A \mid B)"},
            {"type": "table", "format": "html",
             "content": '<table onclick="bad()"><tr><td colspan="2">synthetic</td></tr></table>'}]}]}}}}
    normalized = normalize_result(data, expected_pages=2, page_offset=50)
    assert [p["page_no"] for p in normalized["page_results"]] == [51, 52]
    assert normalized["page_results"][1]["status"] == "empty"
    output = document_markdown(normalized)
    assert 'colspan="2"' in output and "onclick" not in output
    assert r"P(A \mid B)" in output and "原文件第 52 页" in output


class BatchAdapter(Adapter):
    def __init__(self):
        super().__init__()
        self.jobs = {}
        self.fail_second = False

    def submit_document(self, payload, kind):
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(payload) as doc:
            count = len(doc)
        self.calls.append(count)
        ident = "synthetic-batch-" + str(len(self.calls))
        self.jobs[ident] = count
        return ident

    def check_document(self, ident):
        if self.on_check:
            self.on_check()
        if self.fail_second and ident.endswith("-2"):
            raise AdapterError("rate_limited", status=429)
        return {"status": "SUCCESS", "result": {"outputs": {"json": {"pages": [
            {"page_num": n, "blocks": [{"type": "title", "content": "合成章节"},
                                     {"type": "text", "content": "Synthetic source content"}]}
            for n in range(self.jobs[ident])]}}}}


def test_long_document_retries_only_unfinished_batch_and_maps_original_pages(env):
    adapter = BatchAdapter()
    adapter.fail_second = True
    env.service.adapter_factory = lambda *_: adapter
    item = env.service.create(env.owner, validate_document("synthetic.pdf", None, pdf(51)),
        env.config["config_id"], 1, "synthetic-long")
    env.service.run(item["task_id"])
    view = env.service.view(item["material_id"], env.owner)
    assert view["status"] == "failed" and view["processed_pages"] == 50
    assert view["batch_count"] == 2 and adapter.calls == [50, 1]
    assert env.materials.list_for_owner(env.owner) == []
    with pytest.raises(MaterialsError):
        env.service.markdown(item["material_id"], env.owner)
    adapter.fail_second = False
    retry = env.service.retry(item["material_id"], env.owner, env.config["config_id"], 1)
    env.service.run(retry["task_id"])
    assert adapter.calls == [50, 1], "known upstream job must be polled, never resubmitted"
    view = env.service.view(item["material_id"], env.owner)
    assert view["status"] == "parsed" and view["processed_pages"] == 51
    assert view["completed_batches"] == 2
    assert "原文件第 51 页" in env.service.markdown(item["material_id"], env.owner)
    with env.db.session() as sess:
        state = json.loads(sess.get(DocumentAttachmentRow, item["material_id"]).result_json)
        assert [p["page_no"] for p in state["page_results"]] == list(range(1, 52))


def test_long_document_revocation_between_batches_prevents_remaining_outbound(env):
    adapter = BatchAdapter()
    env.service.adapter_factory = lambda *_: adapter
    item = env.service.create(env.owner, validate_document("synthetic.pdf", None, pdf(51)),
        env.config["config_id"], 1, "synthetic-long-revoked")
    adapter.on_check = lambda: env.service.disclosures.revoke_for_config(env.config["config_id"], reason="synthetic")
    env.service.run(item["task_id"])
    assert adapter.calls == [50]
    assert env.service.view(item["material_id"], env.owner)["status"] == "failed"
    with env.db.session() as sess:
        state = json.loads(sess.get(DocumentAttachmentRow, item["material_id"]).result_json)
        assert not state["chunks"] and not state["page_results"]


def test_interrupted_worker_resumes_completed_pages_without_paid_replay(env):
    from datetime import timedelta
    from backend.tasks.service import LearningTaskRow, utcnow
    adapter = BatchAdapter()
    env.service.adapter_factory = lambda *_: adapter
    item = env.service.create(env.owner, validate_document("synthetic.pdf", None, pdf(51)),
        env.config["config_id"], 1, "synthetic-worker-restart")
    adapter.on_check = lambda: env.service.stop.set()
    env.service.run(item["task_id"])
    assert env.service.view(item["material_id"], env.owner)["processed_pages"] == 50
    assert adapter.calls == [50]
    env.service.stop.clear()
    adapter.on_check = None
    with env.db.session() as sess:
        sess.get(LearningTaskRow, item["task_id"]).lease_until = utcnow() - timedelta(seconds=1)
    env.service.run(item["task_id"])
    assert adapter.calls == [50, 1]
    assert env.service.view(item["material_id"], env.owner)["status"] == "parsed"


def test_complex_table_review_is_not_a_claim_of_content_accuracy():
    from backend.materials.document_pipeline import complex_table_pages
    state = {"page_results": [{"page_no": 3, "blocks": [
        {"format": "html", "content": '<table><tr><td colspan="19">Synthetic layout</td></tr></table>'}]}]}
    assert complex_table_pages(state) == [3]


def test_figure_without_ocr_is_visible_instead_of_silently_dropped():
    from backend.materials.document_pipeline import document_markdown, figure_review_pages
    data = {"result": {"outputs": {"json": {"pages": [{"page_num": 0, "blocks": [
        {"type": "figure", "content": "", "img_url": "https://synthetic.test/private"},
        {"type": "figure_caption", "content": "合成流程图"}]}]}}}}
    state = normalize_result(data, expected_pages=1)
    assert figure_review_pages(state) == [1]
    output = document_markdown(state)
    assert "图表内知识可能缺失" in output and "合成流程图" in output
    assert "synthetic.test" not in output

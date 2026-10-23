"""Synthetic local/URL document parity, immutable download snapshots and batch recovery."""
import hashlib
import json
import uuid
from datetime import timedelta

import pytest

from backend.materials.attachments import validate_document
from backend.materials.models import DocumentAttachmentRow, MaterialRow, utcnow
from backend.materials.source_imports import SourceImports
from backend.materials.source_models import ImportedSourceRow
from backend.materials.source_router import ImportItemIn
from backend.materials.storage import MaterialStorage
from backend.materials.study_sources import documents, freeze_sources, verify_sources
from backend.slice1.errors import AdapterError
from backend.slice1.models import PADDLE_VL_ENDPOINT, PADDLE_VL_MODEL
from backend.tasks.service import LearningTaskRow, LeaseError
from test_document_attachments import docx, pdf
from test_source_imports import source_env
from test_text_pptx_attachments import synthetic_pptx


class DocumentAdapter:
    def __init__(self):
        self.calls, self.jobs = [], {}
        self.fail_poll = None
        self.fail_submit = False
        self.crash_submit = False
        self.on_check = None

    def submit_document(self, payload, kind):
        if kind == "pptx":
            from backend.materials.pptx_documents import inspect_pptx
            count = len(inspect_pptx(payload))
        elif kind == "pdf":
            import pypdfium2 as pdfium
            with pdfium.PdfDocument(payload) as document:
                count = len(document)
        else:
            count = 1
        self.calls.append((kind, count))
        job = "synthetic-job-" + str(len(self.calls))
        self.jobs[job] = count
        if self.crash_submit:
            raise LeaseError("LEASE_LOST", "synthetic worker interrupted")
        if self.fail_submit:
            raise AdapterError("timeout")
        return job

    def check_document(self, job):
        if self.on_check:
            self.on_check()
        if job == self.fail_poll:
            raise AdapterError("rate_limited")
        return {"status": "SUCCESS", "result": {"outputs": {"json": {"pages": [
            {"page_num": n, "blocks": [
                {"type": "title", "content": "Synthetic source chapter"},
                {"type": "text", "content": "Why does random sampling reduce selection bias?"}]}
            for n in range(self.jobs[job])]}}}}

    def close(self):
        pass


def configure(env):
    config = env.configs.create(actor_id=env.owner, is_admin=False, kind="document_parser",
        protocol="paddleocr_vl", endpoint=PADDLE_VL_ENDPOINT, model_name=PADDLE_VL_MODEL,
        credentials={"api_key": "synthetic-remote-document-key"})
    adapter = DocumentAdapter()
    env.attachments.adapter_factory = lambda *_: adapter
    return config, adapter


def download(monkeypatch, payload, ext, *, final_ext=None):
    calls = []
    async def fetch(url, deps, *, limit, destination):
        deps.check()
        calls.append(url)
        assert limit == 512_000_000
        destination.write_bytes(payload)
        return len(payload), "application/octet-stream", "https://example.org/document." + (final_ext or ext)
    monkeypatch.setattr("backend.materials.source_clients.fetch_public", fetch)
    return calls


def start(env, ext):
    batch = env.service.create(env.owner, uuid.uuid4().hex, [
        ImportItemIn(client_id="document", url="https://example.org/document." + ext)])
    prepared = env.service.prepare(env.owner, batch["batch_id"])
    view = env.service.commit(env.owner, batch["batch_id"], prepared["manifest_hash"], True)
    return prepared, view["items"][0]["material_id"]


def run(env, material):
    with env.db.session() as session:
        task = session.get(ImportedSourceRow, material).task_id
    env.service.run(task, env.owner, material)
    with env.db.session() as session:
        row = session.get(ImportedSourceRow, material)
        return session.get(MaterialRow, material).status, row.failure_code, json.loads(row.result_json)


def test_remote_document_finishes_after_ocr_without_question_models(source_env, monkeypatch):  # noqa: F811
    from test_material_questions import content_model

    env = source_env
    config, adapter = configure(env)
    content_model(env)
    def forbidden(*args, **kwargs):
        pytest.fail("Default remote documents must not resolve or call question models.")
    monkeypatch.setattr(env.attachments.questions, "defaults", forbidden)
    monkeypatch.setattr(env.attachments.questions, "run", forbidden)
    monkeypatch.setattr("backend.materials.chart_crops.complete_chart_crops", forbidden)
    download(monkeypatch, pdf(2), "pdf")
    prepared, material = start(env, "pdf")
    assert [(d["purpose"], d["config_id"]) for d in prepared["destinations"]] == [
        ("document", config["config_id"])]
    status, failure, state = run(env, material)
    assert (status, failure) == ("parsed", None)
    assert state["study_index"]["sections"]
    assert len(state["page_results"]) == 2
    assert "extraction_binding" not in state and "question_bank" not in state
    assert state["ocr_mode"] == "direct"
    assert adapter.calls == [("pdf", 2)]
    with env.db.session() as session:
        snapshot = freeze_sources(session, env.owner, [material])
        assert verify_sources(session, env.owner, snapshot)


def test_remote_view_retains_question_progress_with_document_metadata(source_env, monkeypatch):
    from backend.materials.source_models import SourceItemRow
    from sqlalchemy import select
    env = source_env
    configure(env)
    download(monkeypatch, pdf(), "pdf")
    _, material = start(env, "pdf")
    with env.db.session() as session:
        row = session.get(ImportedSourceRow, material)
        row.result_json = json.dumps({"unit": "page", "title": "Synthetic progress",
            "question_bank": {"status": "processing"}})
        batch_id = session.scalar(select(SourceItemRow.batch_id).where(
            SourceItemRow.material_id == material))
    item = env.service.view(env.owner, batch_id)["items"][0]
    assert item["question_extraction_status"] == "processing"
    assert item["unit"] == "page" and item["title"] == "Synthetic progress"


@pytest.mark.parametrize("ext", ["txt", "md", "pdf", "docx", "pptx"])
def test_local_and_remote_documents_share_results_and_learning_sources(source_env, monkeypatch, ext):
    env = source_env
    config, adapter = configure(env)
    payload = (pdf(2) if ext == "pdf" else docx() if ext == "docx"
        else synthetic_pptx(hidden=True) if ext == "pptx"
        else b"# Synthetic chapter\n\nWhy does random sampling reduce bias?\n")
    calls = download(monkeypatch, payload, ext)
    prepared, material = start(env, ext)
    local = env.attachments.create(env.owner, validate_document("synthetic." + ext, None, payload),
        config["config_id"], config["config_version"], uuid.uuid4().hex)
    env.attachments.run(local["task_id"])
    status, failure, remote = run(env, material)
    assert (status, failure) == ("parsed", None)
    with env.db.session() as session:
        saved = json.loads(session.get(DocumentAttachmentRow, local["material_id"]).result_json)
        for field in ("chunks", "page_results", "outline", "study_index", "unit"):
            assert remote.get(field) == saved.get(field)
        assert session.get(DocumentAttachmentRow, material) is None
        frozen = freeze_sources(session, env.owner, [material])
        assert verify_sources(session, env.owner, frozen)
        assert documents(session, env.owner, [material])
    assert calls == ["https://example.org/document." + ext]
    assert remote["document_input"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert len(adapter.calls) == (0 if ext in ("txt", "md") else 2)
    assert env.service.view(env.owner, prepared["batch_id"])["status"] == "ready"


def split_pptx(monkeypatch, payload):
    from backend.materials import attachments
    from backend.materials.pptx_documents import pptx_batch_bytes
    original = attachments.plan_pptx_batches
    threshold = max(len(pptx_batch_bytes(payload, n, n)) for n in range(1, 5))
    monkeypatch.setattr(attachments, "plan_pptx_batches", lambda source, **kw:
        original(source, target_bytes=threshold, provider_bytes=threshold + 10, guard=kw.get("guard")))


def retry(env, prepared):
    env.service.retry(env.owner, prepared["batch_id"], prepared["manifest_hash"], True)


def test_remote_pptx_retries_only_unfinished_batches_and_reuses_original(source_env, monkeypatch):
    env = source_env
    _, adapter = configure(env)
    payload = synthetic_pptx(hidden=True)
    calls = download(monkeypatch, payload, "pptx")
    split_pptx(monkeypatch, payload)
    prepared, material = start(env, "pptx")
    adapter.fail_poll = "synthetic-job-2"
    status, code, state = run(env, material)
    assert (status, code) == ("failed", "MODEL_RATE_LIMITED")
    assert [b["status"] for b in state["batches"]][:2] == ["completed", "processing"]
    assert len(state["page_results"]) == 1
    assert state["batches"][1]["job_id"] == "synthetic-job-2"
    env.service = SourceImports(env.attachments)
    adapter.fail_poll = None
    retry(env, prepared)
    status, code, state = run(env, material)
    assert (status, code) == ("parsed", None)
    assert adapter.calls == [("pptx", 1)] * 4 and len(calls) == 1
    assert [page["page_no"] for page in state["page_results"]] == [1, 2, 3, 4]
    assert state["page_results"][0]["hidden"]
    assert len([c for c in state["chunks"] if c["kind"] == "speaker_notes"]) == 1
    assert state["unit"] == "slide"


def test_remote_pdf_keeps_page_batches_on_retry(source_env, monkeypatch):
    env = source_env
    _, adapter = configure(env)
    calls = download(monkeypatch, pdf(51), "pdf")
    prepared, material = start(env, "pdf")
    adapter.fail_poll = "synthetic-job-2"
    assert run(env, material)[0] == "failed"
    adapter.fail_poll = None
    retry(env, prepared)
    status, _, state = run(env, material)
    assert status == "parsed" and len(calls) == 1
    assert adapter.calls == [("pdf", 50), ("pdf", 1)]
    assert [p["page_no"] for p in state["page_results"]] == list(range(1, 52))


def test_remote_worker_reclaim_polls_known_job_without_new_download(source_env, monkeypatch):
    env = source_env
    _, adapter = configure(env)
    calls = download(monkeypatch, pdf(), "pdf")
    _, material = start(env, "pdf")
    adapter.on_check = env.service.stop.set
    status, _, state = run(env, material)
    assert status == "parsing" and state["batches"][0]["status"] == "processing"
    with env.db.session() as session:
        task = session.get(LearningTaskRow, session.get(ImportedSourceRow, material).task_id)
        task.lease_until = utcnow() - timedelta(seconds=1)
    env.service = SourceImports(env.attachments)
    adapter.on_check = None
    assert run(env, material)[0] == "parsed"
    assert adapter.calls == [("pdf", 1)] and len(calls) == 1


def test_unknown_remote_submission_requires_explicit_retry(source_env, monkeypatch):
    env = source_env
    _, adapter = configure(env)
    calls = download(monkeypatch, pdf(), "pdf")
    prepared, material = start(env, "pdf")
    adapter.fail_submit = True
    status, code, state = run(env, material)
    assert (status, code) == ("failed", "OCR_PAGE_TIMEOUT")
    assert state["batches"][0]["job_id"] == "submitting"
    adapter.fail_submit = False
    retry(env, prepared)
    assert run(env, material)[0] == "parsed"
    assert len(adapter.calls) == 2 and len(calls) == 1


def test_download_snapshot_tamper_does_not_refetch_or_submit(source_env, monkeypatch):
    env = source_env
    _, adapter = configure(env)
    calls = download(monkeypatch, pdf(), "pdf")
    prepared, material = start(env, "pdf")
    adapter.fail_poll = "synthetic-job-1"
    assert run(env, material)[0] == "failed"
    env.storage.put(MaterialStorage.original_key(material), pdf(2))
    retry(env, prepared)
    assert run(env, material)[0] == "failed"
    assert len(calls) == len(adapter.calls) == 1


def test_unknown_submit_after_worker_crash_is_not_automatically_replayed(source_env, monkeypatch):
    env = source_env
    _, adapter = configure(env)
    calls = download(monkeypatch, pdf(), "pdf")
    prepared, material = start(env, "pdf")
    adapter.crash_submit = True
    assert run(env, material)[0] == "parsing"
    with env.db.session() as session:
        task = session.get(LearningTaskRow, session.get(ImportedSourceRow, material).task_id)
        task.lease_until = utcnow() - timedelta(seconds=1)
    env.service = SourceImports(env.attachments)
    adapter.crash_submit = False
    status, _, state = run(env, material)
    assert status == "failed" and state["batches"][0]["job_id"] == "submitting"
    assert len(adapter.calls) == len(calls) == 1
    retry(env, prepared)
    assert run(env, material)[0] == "parsed"
    assert len(adapter.calls) == 2 and len(calls) == 1


def test_text_redirect_cannot_acquire_ocr_authorization(source_env, monkeypatch):
    env = source_env
    calls = download(monkeypatch, pdf(), "txt", final_ext="pdf")
    _, material = start(env, "txt")
    status, code, state = run(env, material)
    assert (status, code) == ("failed", "MODEL_CREDENTIAL_INVALID")
    assert len(calls) == 1 and not state.get("batches")


@pytest.mark.parametrize("action", ["revoke", "delete", "cancel"])
def test_remote_guard_between_batches_stops_processing(source_env, monkeypatch, action):
    env = source_env
    config, adapter = configure(env)
    payload = synthetic_pptx()
    download(monkeypatch, payload, "pptx")
    split_pptx(monkeypatch, payload)
    prepared, material = start(env, "pptx")
    def interrupt():
        if action == "revoke":
            env.attachments.disclosures.revoke_for_config(config["config_id"], reason="synthetic")
        elif action == "delete":
            env.attachments.materials.delete(material, actor_id=env.owner)
        else:
            env.service.remove(env.owner, prepared["batch_id"])
    adapter.on_check = interrupt
    status, _, state = run(env, material)
    assert status != "parsed" and adapter.calls == [("pptx", 1)]
    assert not state.get("page_results")


def test_remote_pptx_oversized_slide_is_controlled_before_submit(source_env, monkeypatch):
    env = source_env
    _, adapter = configure(env)
    download(monkeypatch, synthetic_pptx(), "pptx")
    from backend.materials import attachments
    original = attachments.plan_pptx_batches
    monkeypatch.setattr(attachments, "plan_pptx_batches", lambda source, **kw:
        original(source, target_bytes=100, provider_bytes=100, guard=kw.get("guard")))
    _, material = start(env, "pptx")
    status, code, state = run(env, material)
    assert (status, code) == ("failed", "PPTX_SLIDE_TOO_LARGE")
    assert state["failed_slide"] == 1 and adapter.calls == []

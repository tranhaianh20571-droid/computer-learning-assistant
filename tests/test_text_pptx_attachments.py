"""Synthetic native PPTX dependency batches and local text teaching sources."""
import hashlib
import io
import os
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from backend.materials.attachments import create_attachment_router, validate_document
from backend.materials.local_documents import parse_text, MAX_TEXT_BYTES
from backend.materials.pptx_documents import (
    PresentationPackage, inspect_pptx, plan_pptx_batches, pptx_batch_bytes, P, R, REL, CT, A)
from backend.materials.errors import MaterialsError
from backend.materials.models import DocumentAttachmentRow, MaterialPageRow
from backend.materials.router import install_materials_error_handlers
from backend.materials.study_sources import documents, chapter_catalog, freeze_sources, source_candidates, provenance
from backend.planning.retrieval import PlanningRetrievalService
from backend.slice1.errors import AdapterError, Slice1Error
from backend.tasks.service import LeaseError
from test_document_attachments import env as env


def synthetic_pptx(count=4, *, order=None, hidden=False, media_size=4000, external=False):
    order = order or list(range(1, count + 1))
    parts = {}
    def links(items):
        return f'<Relationships xmlns="{REL}">' + "".join(
            f'<Relationship Id="{ident}" Type="{R}/{kind}" Target="{target}"{mode}/>'
            for ident, kind, target, mode in items) + "</Relationships>"
    parts["ppt/presentation.xml"] = (
        f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>' +
        "".join(f'<p:sldId id="{256+i}" r:id="rId{n}"/>' for i, n in enumerate(order)) +
        "</p:sldIdLst></p:presentation>")
    parts["ppt/_rels/presentation.xml.rels"] = links([
        (f"rId{n}", "slide", f"slides/slide{n}.xml", "") for n in range(1, count + 1)])
    for n in range(1, count + 1):
        parts[f"ppt/slides/slide{n}.xml"] = (
            f'<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}" show="{0 if hidden and n == 1 else 1}">'
            f'<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>Synthetic slide {n}</a:t>'
            '</a:r></a:p></p:txBody></p:sp><p:pic><p:blipFill><a:blip r:embed="image"/>'
            '</p:blipFill></p:pic></p:spTree></p:cSld></p:sld>')
        relations = [("layout", "slideLayout", "../slideLayouts/slideLayout1.xml", ""),
                     ("image", "image", f"../media/image{n}.png", "")]
        if n == 1:
            relations.append(("notes", "notesSlide", "../notesSlides/notesSlide1.xml", ""))
        if external:
            relations.append(("external", "hyperlink", "https://synthetic.invalid/", ' TargetMode="External"'))
        parts[f"ppt/slides/_rels/slide{n}.xml.rels"] = links(relations)
        parts[f"ppt/media/image{n}.png"] = os.urandom(media_size)
    parts["ppt/slideLayouts/slideLayout1.xml"] = f'<p:sldLayout xmlns:p="{P}"/>'
    parts["ppt/slideLayouts/_rels/slideLayout1.xml.rels"] = links([
        ("master", "slideMaster", "../slideMasters/slideMaster1.xml", "")])
    parts["ppt/slideMasters/slideMaster1.xml"] = f'<p:sldMaster xmlns:p="{P}"/>'
    parts["ppt/slideMasters/_rels/slideMaster1.xml.rels"] = links([
        ("layout", "slideLayout", "../slideLayouts/slideLayout1.xml", ""),
        ("theme", "theme", "../theme/theme1.xml", "")])
    parts["ppt/theme/theme1.xml"] = f'<a:theme xmlns:a="{A}" name="Synthetic"/>'
    parts["ppt/notesSlides/notesSlide1.xml"] = (
        f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:sp>'
        '<p:txBody><a:p><a:r><a:t>Synthetic private speaker note</a:t>'
        '</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>')
    parts["ppt/notesSlides/_rels/notesSlide1.xml.rels"] = links([
        ("slide", "slide", "../slides/slide1.xml", "")])
    parts["_rels/.rels"] = links([("office", "officeDocument", "ppt/presentation.xml", "")])
    parts["[Content_Types].xml"] = (
        f'<Types xmlns="{CT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="png" ContentType="image/png"/>' +
        "".join(f'<Override PartName="/{path}" ContentType="application/vnd.openxmlformats-officedocument.presentationml.'
                f'{"presentation.main" if path == "ppt/presentation.xml" else "slide"}+xml"/>'
                for path in parts if path.endswith(".xml")) + "</Types>")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, payload in parts.items():
            archive.writestr(path, payload)
    return output.getvalue()


TEXT = "# Synthetic sampling\n\nA population contains all study units.\n\n" \
       "Why does random sampling reduce selection bias?\n\n" \
       "Exercises\n1. Explain how random sampling supports an estimate.\n" \
       "Answer: Synthetic reference answer.\n"


@pytest.mark.parametrize("kind", ["txt", "md"])
def test_local_text_upload_without_configuration_retrieves_and_freezes(env, kind):
    raw = ("\ufeff" + TEXT).encode()
    validated = validate_document("synthetic." + kind, "text/plain", raw)
    item = env.service.create(env.owner, validated, None, None, "local-" + kind)
    env.service.run(item["task_id"])
    view = env.service.view(item["material_id"], env.owner)
    assert view["status"] == "parsed" and view["unit"] == "document" and view["page_count"] == 0
    assert env.adapter.calls == [] and env.service.markdown(item["material_id"], env.owner) == TEXT
    with env.db.session() as session:
        row = session.get(DocumentAttachmentRow, item["material_id"])
        assert row.config_id is None and row.config_version is None
        assert not session.scalars(select(MaterialPageRow).where(
            MaterialPageRow.material_id == item["material_id"])).all()
        assert session.scalar(text("SELECT count(*) FROM disclosure_grants WHERE task_id=:task"),
                              {"task": item["task_id"]}) == 0
        docs = documents(session, env.owner, [item["material_id"]])
        chapters = chapter_catalog(docs)
        snapshot = freeze_sources(session, env.owner, [item["material_id"]],
            [("synthetic-lesson", {"source_section_ids": [chapters[0]["id"]]})])
        candidates = source_candidates(session, env.owner, snapshot, [{"lesson_id": "synthetic-lesson"}])
        assert candidates
        origin = provenance(candidates[0], use="original")
        assert origin["unit"] == "document" and origin["line_start"] > 0
        assert origin["transcription_verified"] is True
    retrieval = PlanningRetrievalService(env.db).assemble(owner_user_id=env.owner,
        material_ids=[item["material_id"]], query="population")
    assert retrieval.excerpts and retrieval.excerpts[0]["unit"] == "document"
    assert retrieval.excerpts[0]["line_start"] > 0
    env.service.remove(item["material_id"], env.owner)
    assert not PlanningRetrievalService(env.db).assemble(owner_user_id=env.owner,
        material_ids=[item["material_id"]], query="population").excerpts


@pytest.mark.parametrize("payload,code", [(b"\xff\xfe", "invalid_encoding"),
    (b"synthetic\x00body", "invalid_file_signature"), (b"  \n", "empty_file"),
    (b"x" * (MAX_TEXT_BYTES + 1), "file_too_large")],
    ids=["encoding", "binary", "empty", "oversize"])
def test_local_validation_rejects_invalid_text(payload, code):
    with pytest.raises(MaterialsError) as caught:
        validate_document("synthetic.txt", None, payload)
    assert caught.value.code == code


def test_markdown_structures_and_exact_line_mapping():
    source = "# Synthetic\n\n```python\nprint('synthetic')\n```\n\n| A | B |\n| - | - |\n| 1 | 2 |\n"
    state = parse_text(source.encode(), "md")
    blocks = [p["blocks"][0] for p in state["page_results"]]
    assert {b["type"] for b in blocks} == {"title", "code", "table"}
    for block in blocks:
        assert source[block["source_start"]:block["source_end"]] == block["content"]
    assert state["study_index"]["sections"][0]["line_start"] == 1
    # Larger than the old 5 MiB restriction, with a long paragraph split safely.
    large = parse_text(b"synthetic " * 600_000, "txt")
    assert large["chunks"] and all(len(c["text"]) <= 2000 for c in large["chunks"])


def test_crlf_offsets_and_oversize_pptx_path(tmp_path):
    raw = "# Synthetic\r\n\r\nWhy does random sampling reduce bias?\r\n"
    state = parse_text(raw.encode(), "md")
    for page in state["page_results"]:
        block = page["blocks"][0]
        assert raw[block["source_start"]:block["source_end"]].replace("\r\n", "\n") == block["content"]
    question = state["study_index"]["questions"][0]
    assert question["line_start"] == 3 and question["line_end"] == 3
    path = tmp_path / "synthetic.pptx"
    with path.open("wb") as output:
        output.truncate(200_000_001)
    with pytest.raises(MaterialsError, match="file_too_large"):
        validate_document(path.name, None, path)


def test_text_structure_budget_is_explicit():
    with pytest.raises(MaterialsError, match="text_too_large"):
        parse_text(("# Synthetic\n" * 5001).encode(), "md")


def test_native_pptx_order_dependency_pruning_hidden_and_notes():
    payload = synthetic_pptx(4, order=[3, 1, 4, 2], hidden=True, external=True)
    slides = inspect_pptx(payload)
    assert [s["part"] for s in slides] == [f"ppt/slides/slide{n}.xml" for n in [3, 1, 4, 2]]
    assert slides[1]["hidden"] and "speaker note" in slides[1]["notes"]
    part = pptx_batch_bytes(payload, 1, 2)
    assert part == pptx_batch_bytes(payload, 1, 2)
    package = PresentationPackage(part)
    try:
        assert len(package.slides) == 2 and not any(s["hidden"] for s in package.slides)
        assert "ppt/media/image1.png" in package.names and "ppt/media/image3.png" in package.names
        assert "ppt/media/image2.png" not in package.names and "ppt/slides/slide2.xml" not in package.names
        assert "ppt/theme/theme1.xml" in package.names
        assert all(b"synthetic.invalid" not in package.archive.read(n)
                   for n in package.names if n.endswith(".rels"))
    finally:
        package.close()


def test_pptx_actual_bytes_batching_and_single_slide_failure():
    payload = synthetic_pptx()
    single_size = max(len(pptx_batch_bytes(payload, n, n)) for n in range(1, 5))
    state = plan_pptx_batches(payload, target_bytes=single_size, provider_bytes=single_size + 10)
    assert state["strategy"] == "batched" and len(state["batches"]) == 4
    for batch in state["batches"]:
        part = pptx_batch_bytes(payload, batch["start"], batch["end"])
        assert len(part) == batch["size_bytes"]
        assert hashlib.sha256(part).hexdigest() == batch["sha256"]
    with pytest.raises(ValueError, match="pptx_slide_too_large"):
        plan_pptx_batches(payload, target_bytes=100, provider_bytes=100)


@pytest.mark.parametrize("mutation", ["macro", "traversal", "missing", "duplicate"])
def test_invalid_pptx_packages_rejected(mutation):
    source = synthetic_pptx(2)
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as original, zipfile.ZipFile(output, "w") as archive:
        for entry in original.infolist():
            if mutation == "missing" and entry.filename == "ppt/media/image1.png":
                continue
            archive.writestr(entry, original.read(entry))
        if mutation == "macro":
            archive.writestr("ppt/vbaProject.bin", b"synthetic")
        elif mutation == "traversal":
            archive.writestr("../synthetic", b"synthetic")
        elif mutation == "duplicate":
            archive.writestr("ppt/presentation.xml", b"synthetic")
    with pytest.raises(MaterialsError):
        validate_document("synthetic.pptx", None, output.getvalue())


def test_local_http_no_confirmation_download_type_and_ocr_gate(env):
    from types import SimpleNamespace
    from fastapi.responses import JSONResponse
    app = FastAPI()
    app.include_router(create_attachment_router(env.service, lambda _: SimpleNamespace(user_id=env.owner)))
    install_materials_error_handlers(app)
    @app.exception_handler(Slice1Error)
    async def controlled(_, exc):
        return JSONResponse({"error_code": exc.code}, status_code=exc.status)
    with TestClient(app) as client:
        preflight = client.post("/api/attachments/preflight",
            files={"file": ("synthetic.txt", TEXT.encode(), "text/plain")})
        assert preflight.json()["requires_ocr"] is False
        response = client.post("/api/attachments", files={"file": ("synthetic.txt", TEXT.encode(), "text/plain")},
            data={"idempotency_key": "synthetic-local-http"})
        assert response.status_code == 201, response.text
        item = response.json()
        env.service.run(item["task_id"])
        downloaded = client.get(f"/api/attachments/{item['material_id']}/markdown")
        assert downloaded.headers["content-type"].startswith("text/plain")
        assert downloaded.headers["content-disposition"].endswith('attachment.txt"')
        assert downloaded.text == TEXT
        rejected = client.post("/api/attachments", files={"file": ("synthetic.pptx",
            synthetic_pptx(), "application/octet-stream")}, data={"idempotency_key": "synthetic-no-consent"})
        assert rejected.status_code == 409 and not env.adapter.calls


class PPTXAdapter:
    def __init__(self):
        self.calls, self.jobs = [], {}
        self.fail_second = True
    def submit_document(self, payload, kind):
        assert kind == "pptx"
        count = len(inspect_pptx(payload))
        self.calls.append(count)
        ident = "synthetic-" + str(len(self.calls))
        self.jobs[ident] = count
        return ident
    def check_document(self, ident):
        if ident == "synthetic-2" and self.fail_second:
            raise AdapterError("rate_limited")
        return {"status": "SUCCESS", "result": {"outputs": {"json": {"pages": [
            {"page_num": n, "blocks": [{"type": "title", "content": "Synthetic chapter"},
                                     {"type": "text", "content": "Why does synthetic sampling reduce bias?"}]}
            for n in range(self.jobs[ident])]}}}}
    def close(self):
        pass


def test_pptx_service_batch_resume_slide_sources(env, monkeypatch):
    from backend.materials import attachments
    original = attachments.plan_pptx_batches
    payload = synthetic_pptx()
    threshold = max(len(pptx_batch_bytes(payload, n, n)) for n in range(1, 5))
    monkeypatch.setattr(attachments, "plan_pptx_batches",
        lambda source, **kw: original(source, target_bytes=threshold, provider_bytes=threshold + 10,
                                     guard=kw.get("guard")))
    adapter = PPTXAdapter()
    env.service.adapter_factory = lambda *_: adapter
    item = env.service.create(env.owner, validate_document("synthetic.pptx", None, payload),
                              env.config["config_id"], 1, "synthetic-pptx-resume")
    env.service.run(item["task_id"])
    view = env.service.view(item["material_id"], env.owner)
    assert view["status"] == "failed" and view["processed_pages"] == 1
    adapter.fail_second = False
    retry = env.service.retry(item["material_id"], env.owner, env.config["config_id"], 1)
    env.service.run(retry["task_id"])
    view = env.service.view(item["material_id"], env.owner)
    assert view["status"] == "parsed" and view["processed_pages"] == 4
    assert adapter.calls == [1, 1, 1, 1]
    assert "第 4 张幻灯片" in env.service.markdown(item["material_id"], env.owner)
    assert "演讲者备注" in env.service.markdown(item["material_id"], env.owner)


def test_local_failure_retry_and_delete_during_parse(env, monkeypatch):
    from backend.materials import attachments
    item = env.service.create(env.owner, validate_document("synthetic.txt", None, TEXT.encode()),
                              None, None, "synthetic-local-retry")
    original = attachments.parse_text
    def fail(*args, **kwargs):
        raise ValueError("synthetic")
    monkeypatch.setattr(attachments, "parse_text", fail)
    env.service.run(item["task_id"])
    assert env.service.view(item["material_id"], env.owner)["failure_code"] == "LOCAL_PARSE_INVALID"
    monkeypatch.setattr(attachments, "parse_text", original)
    retry = env.service.retry(item["material_id"], env.owner, None, None)
    env.service.run(retry["task_id"])
    assert env.service.view(item["material_id"], env.owner)["status"] == "parsed"


def test_delete_during_local_parse_cannot_publish(env, monkeypatch):
    from backend.materials import attachments
    item = env.service.create(env.owner, validate_document("synthetic.txt", None, TEXT.encode()),
                              None, None, "synthetic-local-delete")
    original = attachments.parse_text
    def remove_then_parse(*args, **kwargs):
        env.service.remove(item["material_id"], env.owner)
        return original(*args, **kwargs)
    monkeypatch.setattr(attachments, "parse_text", remove_then_parse)
    try:
        env.service.run(item["task_id"])
    except (LeaseError, MaterialsError):
        pass  # A cancelled lease may reject the failure publication as well.
    with env.db.session() as session:
        assert session.get(DocumentAttachmentRow, item["material_id"]).result_json == "{}"
    with pytest.raises(MaterialsError):
        env.service.view(item["material_id"], env.owner)


def test_nullable_attachment_migration_roundtrip_and_downgrade_guard():
    import importlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect
    migration = importlib.import_module("backend.alembic.versions.f6m7n8o9p0q1_local_attachments")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE document_attachments "
            "(material_id varchar(64) PRIMARY KEY, config_id varchar(64) NOT NULL, config_version integer NOT NULL)"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert all(c["nullable"] for c in inspect(connection).get_columns("document_attachments")
                   if c["name"].startswith("config_"))
        connection.execute(text("INSERT INTO document_attachments VALUES ('synthetic', NULL, NULL)"))
        with Operations.context(MigrationContext.configure(connection)), pytest.raises(RuntimeError):
            migration.downgrade()
        connection.execute(text("DELETE FROM document_attachments"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert all(not c["nullable"] for c in inspect(connection).get_columns("document_attachments")
                   if c["name"].startswith("config_"))
    engine.dispose()


def test_local_question_budget_balances_lessons(env):
    source = "# Synthetic first\n\n" + "\n".join(
        f"Why does synthetic sampling method {i} reduce bias?" for i in range(40))
    source += "\n\n# Synthetic second\n\nWhy does synthetic estimation need a new example?\n"
    item = env.service.create(env.owner, validate_document("synthetic.md", None, source.encode()),
                              None, None, "synthetic-balanced")
    env.service.run(item["task_id"])
    with env.db.session() as session:
        chapters = chapter_catalog(documents(session, env.owner, [item["material_id"]]))
        assert len(chapters) == 2
        snapshot = freeze_sources(session, env.owner, [item["material_id"]], [
            ("first", {"source_section_ids": [chapters[0]["id"]]}),
            ("second", {"source_section_ids": [chapters[1]["id"]]})])
        candidates = source_candidates(session, env.owner, snapshot,
                                       [{"lesson_id": "first"}, {"lesson_id": "second"}])
    assert len(candidates) == 24 and any(c["lesson_ids"] == ["second"] for c in candidates[:2])

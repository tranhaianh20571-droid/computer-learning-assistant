"""Paper playback/media contracts, synthetic content and isolated database only."""
# ruff: noqa: F811
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from backend.planning.models import ConversationRow, utcnow
from backend.teaching import source_reading as reading
from backend.teaching.learning_models import ClassroomRecordingRow
from backend.teaching.models import TeachingStepRow
from backend.teaching.source_figures import figure_cues
from sqlalchemy import select
from test_classroom_interactive import ready
from test_teaching_assessments import database, env  # noqa: F401


@pytest.fixture(autouse=True)
def synthetic_container(monkeypatch):
    from backend.teaching import recordings
    original = recordings.finalize_video
    def finalize(storage, chunks, key):
        if storage.location.write_guard:
            storage.location.write_guard(sum(c["bytes"] for c in chunks)*3)
        with storage.atomic_output(key) as output:
            for item in chunks:
                output.write(storage.get(item["key"]))
    monkeypatch.setattr(recordings, "finalize_video", finalize)
    return original


def test_backward_seek_rejects_forward_and_stale_without_prefetch(env, monkeypatch):
    client, state, _, headers = env
    ident = ready(env)
    with state.db.session() as sess:
        step = sess.scalar(select(TeachingStepRow).where(TeachingStepRow.conversation_id == ident))
    path = f"/api/teaching/{ident}"
    saved = {"client_seq": 1, "step_no": 1, "position_ms": 5000,
        "board_state": {"presentation": {"visible_chars": 30, "paused": True, "playback_version": 0}}}
    assert client.post(path + "/checkpoint", headers=headers, json=saved).status_code == 200
    calls = []
    monkeypatch.setattr(state.teaching, "_ensure_prefetch", lambda *args, **kwargs: calls.append(args))
    target = {**saved, "client_seq": 2, "expected_seq": 1, "position_ms": 0,
        "board_state": {"presentation": {"visible_chars": 10, "paused": True, "playback_version": 0}}}
    response = client.post(path + "/seek-backward", headers=headers, json=target)
    assert response.status_code == 200, response.text
    assert not calls
    target["client_seq"] = 3
    assert client.post(path + "/seek-backward", headers=headers, json=target).status_code == 409
    target["expected_seq"] = 2
    target["board_state"]["presentation"]["visible_chars"] = min(len(step.text), 20)
    assert client.post(path + "/seek-backward", headers=headers, json=target).status_code == 400


def test_recording_chunks_idempotent_owned_and_complete(env):
    client, state, owner, headers = env
    ident = ready(env)
    path = f"/api/teaching/{ident}/recordings"
    created = client.post(path, headers=headers, json={})
    assert created.status_code == 200, created.text
    record = created.json()["recording_id"]
    assert client.post(path, headers=headers, json={}).status_code == 409
    chunks = [b"\x1a\x45\xdf\xa3synthetic-header", b"synthetic-tail"]
    for sequence, chunk in enumerate(chunks):
        response = client.put(f"{path}/{record}/chunks/{sequence}", headers=headers, content=chunk)
        assert response.status_code == 200, response.text
        assert client.put(f"{path}/{record}/chunks/{sequence}", headers=headers, content=chunk).json()["deduped"]
    assert client.put(f"{path}/{record}/chunks/0", headers=headers, content=b"changed").status_code == 409
    response = client.post(f"{path}/{record}/finish", headers=headers,
        json={"duration_ms": 1000, "events": [{"at_ms": 0, "step_no": 1, "private": "discard"}]})
    assert response.status_code == 200, response.text
    video = client.get(f"{path}/{record}/video", headers=headers)
    assert video.content == b"".join(chunks)
    assert video.headers["cache-control"] == "no-store"
    with state.db.session() as sess:
        row = sess.get(ClassroomRecordingRow, record)
        assert row.owner_user_id == owner
        assert "private" not in row.events_json
        from backend.cleanup.inventory import describe
        assert describe(sess, row.storage_key) == ("recordings", owner, 1)
    assert client.delete(f"{path}/{record}", headers=headers).status_code == 200
    assert client.get(f"{path}/{record}/video", headers=headers).status_code == 404
    assert not list(state.teaching.storage.iter_files(f"recordings/{ident}/{record}/"))


def test_deleted_conversation_fences_late_recording_upload(env):
    client, state, _, headers = env
    ident = ready(env)
    path = f"/api/teaching/{ident}/recordings"
    record = client.post(path, headers=headers, json={}).json()["recording_id"]
    with state.db.session() as sess:
        sess.get(ConversationRow, ident).deleted_at = utcnow()
    assert client.put(f"{path}/{record}/chunks/0", headers=headers, content=b"\x1a\x45\xdf\xa3s").status_code == 404
    assert client.post(f"{path}/{record}/finish", headers=headers, json={"duration_ms": 0}).status_code == 404


def test_recording_rejects_other_owner_and_keeps_failed_write_recoverable(env, monkeypatch):
    import json

    from backend.materials.storage import StorageError
    from common_fixtures import auth_headers, make_user
    client, state, _, headers = env
    ident = ready(env)
    path = f"/api/teaching/{ident}/recordings"
    record = client.post(path, headers=headers, json={}).json()["recording_id"]
    _, token = make_user(client, state.db)
    other = auth_headers(token)
    assert client.get(path, headers=other).status_code == 404
    assert client.put(f"{path}/{record}/chunks/0", headers=other, content=b"data").status_code == 404
    original = state.teaching.storage.put
    def full(*args):
        raise StorageError("local_storage_full")
    monkeypatch.setattr(state.teaching.storage, "put", full)
    payload = b"\x1a\x45\xdf\xa3synthetic-recoverable"
    assert client.put(f"{path}/{record}/chunks/0", headers=headers, content=payload).status_code == 503
    with state.db.session() as sess:
        row = sess.get(ClassroomRecordingRow, record)
        assert row.size_bytes == 0 and json.loads(row.chunks_json) == []
    monkeypatch.setattr(state.teaching.storage, "put", original)
    assert client.put(f"{path}/{record}/chunks/0", headers=headers, content=payload).status_code == 200
    location = state.teaching.storage.location
    monkeypatch.setattr(location, "write_guard", full)
    assert client.post(f"{path}/{record}/finish", headers=headers, json={"duration_ms": 100}).status_code == 503
    monkeypatch.setattr(location, "write_guard", None)
    response = client.post(f"{path}/{record}/finish", headers=headers,
        json={"duration_ms": 0, "complete": False})
    assert response.status_code == 200 and response.json()["status"] == "interrupted"
    assert client.get(f"{path}/{record}/video", headers=other).status_code == 404
    assert client.get(f"{path}/{record}/video", headers=headers).content == payload


def test_recording_delete_respects_existing_resource_pin(env):
    from backend.cleanup.models import LocalResourceRow
    client, state, owner, headers = env
    ident = ready(env)
    path = f"/api/teaching/{ident}/recordings"
    record = client.post(path, headers=headers, json={}).json()["recording_id"]
    assert client.put(f"{path}/{record}/chunks/0", headers=headers, content=b"\x1a\x45\xdf\xa3synthetic").status_code == 200
    with state.db.session() as sess:
        sess.add(LocalResourceRow(resource_id="synthetic-recording-pin", owner_user_id=owner,
            object_key=f"recordings/{ident}/{record}/000000.webm",
            volume_id=state.teaching.storage.location.volume_id, kind="recordings", pinned=True))
    assert client.delete(f"{path}/{record}", headers=headers).status_code == 409
    with state.db.session() as sess:
        sess.get(LocalResourceRow, "synthetic-recording-pin").pinned = False
    assert client.delete(f"{path}/{record}", headers=headers).status_code == 200


def test_lesson_review_is_owned_versioned_and_read_only(env):
    from backend.planning.models import CourseRow, LessonProgressRow
    from common_fixtures import auth_headers, make_user
    client, state, owner, headers = env
    ident = ready(env)
    with state.db.session() as sess:
        conv = sess.get(ConversationRow, ident)
        course_id, lesson_id = conv.course_id, conv.lesson_id
        version = conv.lesson_content_version_id
        baseline = [(p.learning_state, p.lesson_content_version_id) for p in sess.scalars(
            select(LessonProgressRow).where(LessonProgressRow.owner_user_id == owner))]
    path = f"/api/courses/{course_id}/lessons/{lesson_id}/recordings"
    assert client.get(path, headers=headers).json()["data"] == []
    recorded = client.post(f"/api/teaching/{ident}/recordings", headers=headers, json={}).json()
    response = client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["recording_id"] == recorded["recording_id"]
    _, token = make_user(client, state.db)
    assert client.get(path, headers=auth_headers(token)).status_code == 404
    with state.db.session() as sess:
        row = sess.get(ClassroomRecordingRow, recorded["recording_id"])
        row.content_version_id = "obsolete-content"
    assert client.get(path, headers=headers).json()["data"] == []
    with state.db.session() as sess:
        sess.get(ClassroomRecordingRow, recorded["recording_id"]).content_version_id = version
        sess.get(ConversationRow, ident).deleted_at = utcnow()
    assert client.get(path, headers=headers).json()["data"] == []
    with state.db.session() as sess:
        current = [(p.learning_state, p.lesson_content_version_id) for p in sess.scalars(
            select(LessonProgressRow).where(LessonProgressRow.owner_user_id == owner))]
        assert current == baseline
        sess.get(CourseRow, course_id).deleted_at = utcnow()
    assert client.get(path, headers=headers).status_code == 404


def test_real_webm_is_seekable_and_ranges_are_private(env, monkeypatch, tmp_path, synthetic_container):
    import subprocess

    import imageio_ffmpeg
    from backend.teaching import recordings
    # The other tests use deliberately synthetic, invalid media containers.
    monkeypatch.setattr(recordings, "finalize_video", synthetic_container)
    client, state, _, headers = env
    ident = ready(env)
    source = tmp_path / "fixture.webm"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=12",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "2", "-c:v", "libvpx", "-c:a", "libopus", "-f", "webm", str(source)],
        check=True, capture_output=True, timeout=30)
    path = f"/api/teaching/{ident}/recordings"
    record = client.post(path, headers=headers, json={}).json()["recording_id"]
    raw = source.read_bytes()
    for sequence, offset in enumerate(range(0, len(raw), 16000)):
        assert client.put(f"{path}/{record}/chunks/{sequence}", headers=headers,
            content=raw[offset:offset+16000]).status_code == 200
    saved = client.post(f"{path}/{record}/finish", headers=headers,
        json={"duration_ms":2000, "complete":False})
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "interrupted"
    whole = client.get(f"{path}/{record}/video", headers=headers)
    assert whole.status_code == 200 and b"\x1c\x53\xbb\x6b" in whole.content
    assert whole.headers["content-disposition"].startswith("inline;")
    partial = client.get(f"{path}/{record}/video", headers={**headers,"Range":"bytes=100-299"})
    assert partial.status_code == 206 and partial.content == whole.content[100:300]
    assert partial.headers["cache-control"] == "no-store"
    target = tmp_path / "finished.webm"
    target.write_bytes(whole.content)
    probe = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-i", str(target),
        "-ss", "1", "-t", "0.2", "-f", "null", "-"], capture_output=True, timeout=30, check=False)
    assert probe.returncode == 0 and b"Duration: 00:00:02" in probe.stderr
    assert not list(state.teaching.storage.iter_files(f"recordings/{ident}/{record}/000000"))


def test_figure_sentence_cues_use_unique_document_target():
    target = {"material_id": "paper", "figure_no": "1", "target_id": "p4_b2", "page_no": 4}
    text = "先读正文。如图一所示，这里比较了方法与结果。再回到正文。"
    cues = figure_cues(text, [target], "paper")
    assert len(cues) == 1 and cues[0]["pdf_focus"] == target
    assert "如图一" in text[cues[0]["start"]:cues[0]["end"]]
    assert figure_cues(text, [target, {**target, "page_no": 5}], "paper") == []
    assert figure_cues(text, [target], "another-paper") == []


def test_figure_caption_from_opening_uses_native_coordinates_not_processed_crop(monkeypatch):
    import json

    from backend.teaching import interactive
    from backend.teaching.source_figures import figure_targets
    pages = [{"page_no": 2, "blocks": [{"type": "figure", "content": "Figure 1 | Synthetic diagram",
        "processed_bbox": [.1, .2, .8, .4], "crop": {"sha256": "b"*64}}]}]
    service = SimpleNamespace(db=SimpleNamespace(session=lambda: nullcontext(
        SimpleNamespace(get=lambda *args: SimpleNamespace(result_json=json.dumps({"page_results": pages}))))))
    conv = SimpleNamespace(owner_user_id="owner", lesson_id="method")
    doc = {"material_id": "paper", "sha256": "a"*64, "index": {"sections":[
        {"id":"method", "page_no":4, "end_page":4}]}}
    context = {"source_snapshot":{"reading_lessons":["method"], "lessons":{"method":["paper/method"]}}}
    caption = {"material_id":"paper", "target_id":"p2_r1", "text":"Figure 1 | Synthetic diagram",
        "page_no":2, "page_count":4, "precision":"text", "bbox":[.1,.5,.8,.52]}
    monkeypatch.setattr(interactive, "source_path", lambda *args:"synthetic.pdf")
    monkeypatch.setattr(interactive, "pdf_page_targets", lambda *args:[caption])
    targets = figure_targets(service, conv, context, [doc])
    assert len(targets) == 1 and targets[0]["bbox"] == caption["bbox"]
    assert targets[0]["precision"] == "text" and "crop_sha256" not in targets[0]


def test_opening_keeps_title_and_abstract_before_introduction():
    from backend.materials import study_sources
    from backend.teaching import interactive
    title = "Synthetic Research into Reliable Learning"
    abstract = "We study reliable learning and compare alternative approaches. " * 4
    intro = "This introduction explains the central research problem and the chosen method."
    def paragraph(number, text):
        return {"material_id": "paper", "sha256": "a"*64, "target_id": f"p1_para{number}", "page_no": 1,
            "page_count": 1, "text": text, "bbox": [.1, .2, .8, .3], "precision": "paragraph",
            "regions": [], "char_boxes": [None] * len(text)}
    paragraphs = [paragraph(0, title), paragraph(1, abstract), paragraph(2, "1. Introduction"), paragraph(3, intro)]
    sections = [{"id": "intro", "title": "1. Introduction", "offset": 100, "page_no": 1, "end_page": 1}]
    docs = [{"material_id": "paper", "sha256": "a"*64, "index": {"sections": sections}}]
    context = {"material_ids": ["paper"], "source_sections": sections, "scope_capabilities": [],
        "source_snapshot": {"lessons": {"lesson": ["paper/intro"]}},
        "lesson_title": "Synthetic paper", "lesson_goal": "Understand the paper"}
    service = SimpleNamespace(db=SimpleNamespace(session=lambda: nullcontext(None)))
    conv = SimpleNamespace(owner_user_id="owner", lesson_id="lesson")
    with patch.object(study_sources, "documents", return_value=docs), patch.object(study_sources, "verify_sources"), \
            patch.object(interactive, "source_path", return_value="unused"), \
            patch.object(reading, "pdf_paragraphs", return_value=paragraphs), \
            patch("backend.teaching.source_figures.figure_targets", return_value=[]):
        plan = reading.build_source_reading_plan(service, conv, context)
        assert plan.units[0].source_text == title
        assert any(unit.context_text == abstract for unit in plan.units)
        context["include_paper_opening"] = False
        scoped = reading.build_source_reading_plan(service, conv, context)
        assert scoped.units[0].source_text == intro


def test_first_method_lesson_gets_opening_without_scope_credit():
    from backend.materials import study_sources
    from backend.teaching import interactive
    title = "Synthetic Research into Reliable Learning"
    def paragraph(page, number, text):
        return {"material_id":"paper", "sha256":"a"*64, "target_id":f"p{page}_para{number}",
            "page_no":page, "page_count":4, "text":text, "bbox":[.1,.2,.8,.3],
            "precision":"paragraph", "regions":[], "char_boxes":[None]*len(text)}
    pages = {1:[paragraph(1,0,title),paragraph(1,1,"We explain the research question and outline the main findings. "*4)],
        2:[],3:[],4:[paragraph(4,0,"2. Method"),paragraph(4,1,"The method compares competing policies to improve reliable learning outcomes. "*4)]}
    sections = [{"id":"method","title":"2. Method","offset":1000,"page_no":4,"end_page":4}]
    docs = [{"material_id":"paper","sha256":"a"*64,"index":{"sections":sections}}]
    context = {"material_ids":["paper"],"source_sections":sections,
        "scope_capabilities":[{"point_label":"Method","capability":"Compare policies","capability_id":"method-only"}],
        "source_snapshot":{"reading_lessons":["lesson"],"lessons":{"lesson":["paper/method"]}},
        "lesson_title":"Paper method","lesson_goal":"Compare policies"}
    service = SimpleNamespace(db=SimpleNamespace(session=lambda:nullcontext(None)))
    conv = SimpleNamespace(owner_user_id="owner",lesson_id="lesson")
    with patch.object(study_sources,"documents",return_value=docs),patch.object(study_sources,"verify_sources"), \
            patch.object(interactive,"source_path",return_value="unused"), \
            patch.object(reading,"pdf_paragraphs",side_effect=lambda *args:pages[args[-1]]), \
            patch("backend.teaching.source_figures.figure_targets",return_value=[]):
        plan = reading.build_source_reading_plan(service,conv,context)
        assert plan.version == 2 and plan.units[0].source_text == title
        assert plan.units[0].knowledge_point_id == "paper_opening"
        assert not any(u.scope_capability_ids for u in plan.units if u.knowledge_point_id == "paper_opening")
        assert any(u.scope_capability_ids == ["method-only"] for u in plan.units if u.section_role == "core_method")

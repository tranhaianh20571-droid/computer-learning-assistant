"""Local character-material parsing and owner-scoped, resumable dialogue turns."""

import io
import json
import uuid
import zipfile

import pytest
from backend.characters.dialogue import extract_material
from backend.characters.distillation_models import BuildRequest, CharacterBuildRow
from backend.slice1.errors import Slice1Error
from backend.tasks.service import LearningTaskRow
from test_persona_workflows import builder

pytest_plugins = ("test_character_cards",)


def docx_bytes(xml=None, extra=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            xml
            or (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>林老师先问学生怎么想。</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>然后给一个小提示。</w:t></w:r></w:p></w:body></w:document>"
            ),
        )
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return output.getvalue()


def test_character_material_accepts_txt_and_extracts_docx_paragraphs():
    assert extract_material("notes.txt", "\ufeff人物材料".encode()) == {
        "text": "人物材料",
        "characters": 4,
    }
    parsed = extract_material("person.docx", docx_bytes())
    assert parsed["text"] == "林老师先问学生怎么想。\n然后给一个小提示。"
    assert parsed["characters"] == len(parsed["text"])


@pytest.mark.parametrize(
    "filename,payload",
    [
        ("person.pdf", b"%PDF-1.7"),
        ("empty.txt", b" \n\t"),
        ("binary.txt", b"\0body"),
        ("bad.docx", b"not a zip"),
        ("macro.docx", docx_bytes(extra={"word/vbaProject.bin": b"macro"})),
        (
            "dtd.docx",
            docx_bytes(
                '<!DOCTYPE x [<!ENTITY a "expanded">]><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:t>&a;</w:t></w:p></w:body></w:document>'
            ),
        ),
    ],
)
def test_character_material_rejects_unsupported_or_active_documents(filename, payload):
    with pytest.raises(Slice1Error) as failure:
        extract_material(filename, payload)
    assert failure.value.code in {"invalid_field", "unsupported_media_type"}


def test_character_material_caps_body_and_upload_size():
    with pytest.raises(Slice1Error) as too_long:
        extract_material("large.txt", ("x" * 180001).encode())
    assert too_long.value.status == 413
    with pytest.raises(Slice1Error) as too_large:
        extract_material("large.txt", b"x" * 10_000_001)
    assert too_large.value.status == 413


def test_dialogue_builds_continue_the_same_private_card_and_reject_replay(env, monkeypatch):
    cards, owner, service, _ = builder(env, monkeypatch, pipeline_version=7)
    monkeypatch.setattr(service, "kick", lambda ident: None)
    first = service.create(
        owner,
        BuildRequest(
            mode="ai",
            dialogue=True,
            topic="待识别人物",
            message="我想创建一位数学导师。",
            request_key=uuid.uuid4().hex,
            confirmed=True,
        ),
    )
    with cards.db.session() as sess:
        row = sess.get(CharacterBuildRow, first["build_id"])
        task = sess.get(LearningTaskRow, row.task_id)
        task.status = "succeeded"
        row.result_json = json.dumps(
            {
                "dialogue": {
                    "reply": "先理解学生的思路。",
                    "question": "请举个例子。",
                    "question_count": 1,
                    "profile": {"name": "合成导师"},
                    "skill": "Synthetic skill",
                }
            }
        )
        row.sources_json = json.dumps(
            [{"id": "source_synthetic", "kind": "unknown", "text": "Synthetic source"}]
        )
    continued = service.create(
        owner,
        BuildRequest(
            mode="ai",
            dialogue=True,
            topic="待识别人物",
            parent_build_id=first["build_id"],
            message="先问理由，再给提示。",
            request_key=uuid.uuid4().hex,
            confirmed=True,
        ),
    )
    assert continued["card_id"] == first["card_id"]
    assert continued["request"]["dialogue_history"] == [
        {"role": "user", "content": "我想创建一位数学导师。"},
        {"role": "assistant", "content": "先理解学生的思路。\n请举个例子。"},
    ]
    assert continued["source_count"] == 1
    assert continued["request"]["question_count"] == 1
    assert continued["request"]["research_policy"]["max_searches"] == 5
    with pytest.raises(Slice1Error) as replay:
        service.create(
            owner,
            BuildRequest(
                mode="ai",
                dialogue=True,
                topic="待识别人物",
                parent_build_id=first["build_id"],
                message="第二个并发续轮。",
                request_key=uuid.uuid4().hex,
                confirmed=True,
            ),
        )
    assert replay.value.status == 409
    with pytest.raises(Slice1Error):
        service.create(
            "synthetic-other",
            BuildRequest(
                mode="ai",
                dialogue=True,
                topic="待识别人物",
                parent_build_id=first["build_id"],
                message="越权续轮",
                request_key=uuid.uuid4().hex,
                confirmed=True,
            ),
        )


def test_dialogue_requires_explicit_model_egress_confirmation(env, monkeypatch):
    _, owner, service, _ = builder(env, monkeypatch, pipeline_version=7)
    monkeypatch.setattr(service, "kick", lambda ident: None)
    request = BuildRequest(
        mode="ai",
        dialogue=True,
        topic="Synthetic",
        message="Create a tutor.",
        request_key=uuid.uuid4().hex,
        confirmed=False,
    )
    with pytest.raises(Slice1Error) as failure:
        service.create(owner, request)
    assert failure.value.status == 409


def test_material_dialogue_stops_after_eight_model_questions(env, monkeypatch):
    from backend.characters.dialogue import DialoguePlan, DialogueReply
    from backend.characters.schemas import Profile

    _, owner, service, _ = builder(env, monkeypatch, pipeline_version=7)
    monkeypatch.setattr(service, "kick", lambda ident: None)
    replies = []

    def stage(root, key, payload, output_type, prompt, *, work=None):
        if key == "dialogue_plan":
            return DialoguePlan(topic="Synthetic mentor", subject_kind="real")
        if key == "dialogue_reply":
            remaining = payload["questions_remaining"]
            reply = DialogueReply(
                reply="Synthetic grounded response.",
                question="One bounded question?" if remaining else "",
                profile=Profile(name="Synthetic mentor"),
                skill="# Synthetic mentor\n" + "Grounded mechanism and limits.\n" * 14,
                thinking_mechanism="Check the stated conditions.",
                decision_method="Ask for the learner's reasoning.",
                expression_style="Brief and precise.",
                knowledge_boundary="Leave unsupported facts unknown.",
            )
            replies.append(reply.question)
            return reply
        raise AssertionError(key)

    monkeypatch.setattr(service, "_stage", stage)
    parent = ""
    for index in range(9):
        created = service.create(
            owner,
            BuildRequest(
                mode="ai",
                dialogue=True,
                topic="Synthetic mentor",
                message=f"Synthetic turn {index}.",
                source_text="Synthetic source material.",
                parent_build_id=parent,
                request_key=uuid.uuid4().hex,
                confirmed=True,
            ),
        )
        result = service.run(created["build_id"])
        assert result.get("status") == "succeeded", result
        saved = service.get(owner, created["build_id"])
        assert saved["result"]["dialogue"]["question_count"] == min(index + 1, 8)
        parent = created["build_id"]
    assert sum(bool(question) for question in replies) == 8
    assert replies[-1] == ""


def dialogue_search_builder(env, monkeypatch, *, before_result=None):
    import test_persona_workflows
    from backend.auth.models import UserRow
    from backend.search.service import SearchService
    from test_anysearch_service import Adapter

    cards, owner, service, calls = builder(env, monkeypatch, pipeline_version=7)
    with cards.db.session() as sess:
        sess.add(UserRow(user_id=owner, email=f"{owner}@example.test",
                         password_hash="synthetic", status="approved"))
    view = cards.configs.create(
        actor_id=owner, is_admin=False, kind="search", protocol="anysearch",
        endpoint="https://api.anysearch.com", model_name="",
        credentials={"api_key": "synthetic-search-key"})
    adapter = Adapter(before_result=before_result)
    service.search_factory = lambda user, task: SearchService(
        cards.db, cards.configs, owner_user_id=user, task_id=task,
        adapter_factory=lambda *_: adapter)
    monkeypatch.setattr(service, "_read_source", lambda *_: "Synthetic public evidence. " * 40)
    original = test_persona_workflows.synthetic_reply

    def reply(stage, payload, props):
        if stage == "dialogue_plan":
            return {"topic": "Synthetic mentor", "subject_kind": "real",
                    "queries": ["Synthetic mentor public interview" + (
                        " followup" if payload["message"] == "Synthetic turn 1" else "")]}
        if stage == "dialogue_reply":
            return {
                "reply": "Synthetic grounded response.", "question": "Which habit?",
                "profile": {"name": "Synthetic mentor"},
                "skill": test_persona_workflows.SKILL,
                "thinking_mechanism": "Check conditions.", "decision_method": "Ask for reasons.",
                "expression_style": "Brief.", "knowledge_boundary": "Unknown stays unknown.",
                "ready": False,
            }
        return original(stage, payload, props)

    monkeypatch.setattr(test_persona_workflows, "synthetic_reply", reply)
    return cards, owner, service, adapter, view, calls


def test_dialogue_search_succeeds_on_first_attempt_and_does_not_repeat_after_resume(env, monkeypatch):
    from backend.characters.distillation_models import CharacterBuildStageRow
    from backend.search.repository import SearchCallRow
    from sqlalchemy import select

    cards, owner, service, adapter, _, _ = dialogue_search_builder(env, monkeypatch)
    parent = ""
    for index in range(2):
        created = service.create(owner, BuildRequest(
            mode="ai", dialogue=True, topic="Synthetic mentor",
            message=f"Synthetic turn {index}", parent_build_id=parent,
            confirmed=True, request_key=uuid.uuid4().hex))
        receipt = service.run(created["build_id"])
        assert receipt["status"] == "succeeded", receipt
        saved = service.get(owner, created["build_id"])
        assert saved["source_count"] == 1
        with cards.db.session() as sess:
            assert sess.get(LearningTaskRow, created["task_id"]).attempt == 1
            stages = list(sess.scalars(select(CharacterBuildStageRow).where(
                CharacterBuildStageRow.build_id == created["build_id"])))
            searches = [row for row in stages if row.stage_key.startswith("dialogue_sources_")]
            assert len(searches) == 1
            stage = searches[0]
            assert stage.stage_key == f"dialogue_sources_{index}_0"
            assert sess.get(LearningTaskRow, stage.task_id).attempt == 1
            state = json.loads(stage.state_json)
            assert state["search_metrics"]["completed_actions"] == 1
            assert not state.get("interrupted") and not state.get("error_code")
            record = sess.scalars(select(SearchCallRow).where(
                SearchCallRow.task_id == stage.task_id)).one()
            assert record.state == "succeeded" and record.result_count == 1
            assert saved["result"]["dialogue"]["search_note"] == "本轮完成 1 次搜索"
        service.run(created["build_id"])
        parent = created["build_id"]
    assert adapter.calls == 2


@pytest.mark.parametrize("stage_key", [
    "dialogue_plan", "dialogue_reply", "delivery_image_neutral",
    "dialogue_sources_bad", "dialogue_sources_0_0_fallback",
])
def test_dialogue_search_binding_rejects_non_collection_stages(env, monkeypatch, stage_key):
    from backend.characters.distillation_models import CharacterBuildStageRow
    from backend.planning.errors import PlanningError

    cards, owner, service, adapter, _, _ = dialogue_search_builder(env, monkeypatch)
    created = service.create(owner, BuildRequest(
        mode="ai", dialogue=True, topic="Synthetic mentor", message="Synthetic request",
        confirmed=True, request_key=uuid.uuid4().hex))
    service.tasks.claim(created["task_id"], lease_owner="synthetic-root")
    child = service.tasks.create_task(
        owner_user_id=owner, kind="character_build_stage", subject_id=created["build_id"],
        idempotency_key=uuid.uuid4().hex, stage="search")
    with cards.db.session() as sess:
        sess.add(CharacterBuildStageRow(
            task_id=child["task_id"], build_id=created["build_id"], stage_key=stage_key,
            input_hash="synthetic", state_json=json.dumps({"query": "Synthetic mentor"})))
    service.tasks.claim(child["task_id"], lease_owner="synthetic-search")
    with pytest.raises(PlanningError) as failure:
        service.search_factory(owner, child["task_id"]).attempt("Synthetic mentor")
    assert failure.value.code == "TASK_CANCELLED" and adapter.calls == 0


@pytest.mark.parametrize("fence", ["cancel", "delete", "config"])
def test_dialogue_search_still_rejects_control_changes_before_publication(env, monkeypatch, fence):
    cards, owner, service, adapter, view, _ = dialogue_search_builder(env, monkeypatch)
    created = service.create(owner, BuildRequest(
        mode="ai", dialogue=True, topic="Synthetic mentor", message="Synthetic request",
        confirmed=True, request_key=uuid.uuid4().hex))

    def change():
        if fence == "cancel":
            service.action(owner, created["build_id"], "cancel")
        elif fence == "delete":
            cards.delete(owner, created["card_id"])
        else:
            cards.configs.update(
                view["config_id"], actor_id=owner, is_admin=False,
                credentials={"api_key": "synthetic-rotated-key"})

    adapter.before_result = change
    assert service.run(created["build_id"])["status"] != "succeeded"
    assert adapter.calls == 1
    with cards.db.session() as sess:
        row = sess.get(CharacterBuildRow, created["build_id"])
        assert json.loads(row.sources_json) == [] and row.accepted_revision is None

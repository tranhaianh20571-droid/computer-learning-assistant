"""Atomic mentor changes: preserve past work, fence and rebuild future work."""
import json
import uuid
from sqlalchemy import select, func
from .models import CharacterBindingRow, CharacterSegmentRow, ClassroomPlaybackRow, ClassroomHistoryRow
from ..planning.models import load_json_field
from ..teaching.models import TeachingStepRow, PlaybackCheckpointRow
from ..teaching.learning_models import LessonBlueprintRow, TaughtKnowledgeRow, FeynmanSessionRow
from ..tasks.service import LearningTaskRow
from ..prompts.service import PromptBindingRow, PromptUnavailable


def playback_version(sess, conversation_id):
    row = sess.get(ClassroomPlaybackRow, conversation_id)
    return row.version if row else 0


def switch_mentor(teaching, sess, conv, view, profile):
    state = sess.get(ClassroomPlaybackRow, conv.conversation_id, with_for_update=True)
    snapshot = json.dumps({"card_id":view["card_id"],"revision":view["revision"],"profile":profile}
        if view else {}, ensure_ascii=False, sort_keys=True)
    if state and state.profile_json == snapshot:
        return state.effective_step_no
    point = sess.scalar(select(PlaybackCheckpointRow).where(
        PlaybackCheckpointRow.conversation_id == conv.conversation_id).order_by(PlaybackCheckpointRow.client_seq.desc()))
    presentation = load_json_field(point.board_state_json, {}).get("presentation", {}) if point else {}
    effective = max(1, point.step_no if point else 1)+int(bool(presentation.get("text_complete")))
    completed = sess.scalar(select(func.max(TaughtKnowledgeRow.after_step_no)).where(
        TaughtKnowledgeRow.conversation_id == conv.conversation_id)) or 0
    quiz = sess.scalar(select(FeynmanSessionRow).where(FeynmanSessionRow.conversation_id == conv.conversation_id,
        FeynmanSessionRow.state != "closed").order_by(FeynmanSessionRow.created_at.desc()))
    effective = max(effective, completed+1, quiz.after_step_no+1 if quiz else 1)
    steps = list(sess.scalars(select(TeachingStepRow).where(
        TeachingStepRow.conversation_id == conv.conversation_id, TeachingStepRow.generation == 1)))
    from .service import segment_character
    for step in steps:
        if step.step_no < effective:
            segment_character(sess, conv.conversation_id, step.step_no, freeze=True)
    if state is None:
        state = ClassroomPlaybackRow(conversation_id=conv.conversation_id, version=0,
            effective_step_no=effective, profile_json="{}")
        sess.add(state)
    state.version += 1
    state.effective_step_no, state.profile_json = effective, snapshot
    version = state.version
    for task in sess.scalars(select(LearningTaskRow).where(
            LearningTaskRow.owner_user_id == conv.owner_user_id,
            LearningTaskRow.subject_id.like(conv.conversation_id+":%"),
            LearningTaskRow.kind.in_(("lesson_step_generate", "tts")))):
        parts = task.subject_id.split(":")
        if len(parts) == 3 and parts[1].isdigit() and int(parts[1]) >= effective:
            task.cancel_requested = True
    for step in steps:
        if step.step_no < effective:
            continue
        old = sess.get(CharacterSegmentRow, (conv.conversation_id, step.step_no))
        data = {column.name:getattr(step,column.name) for column in step.__table__.columns}
        data["created_at"] = str(data["created_at"])
        data["character"] = {"card_id":old.card_id,"revision":old.revision,"profile_json":old.profile_json,
            "expression_json":old.expression_json} if old else None
        sess.add(ClassroomHistoryRow(conversation_id=conv.conversation_id, version=version,
            step_no=step.step_no, snapshot_json=json.dumps(data, ensure_ascii=False)))
        if old:
            sess.delete(old)
        step.text_version_no += 1
        step.text, step.title, step.status = "", "", "generating"
        step.board_scene_id, step.audio_id, step.lesson_finished = None, None, False
    binding = sess.get(CharacterBindingRow, conv.conversation_id)
    if view:
        if binding is None:
            binding = CharacterBindingRow(conversation_id=conv.conversation_id)
            sess.add(binding)
        binding.card_id, binding.revision = view["card_id"], view["revision"]
        binding.profile_json, binding.effective_step_no = json.dumps(profile, ensure_ascii=False), effective
    elif binding:
        sess.delete(binding)
    sess.flush()
    blueprint = sess.get(LessonBlueprintRow, conv.conversation_id)
    from ..teaching.learning_outputs import LessonBlueprint, teaching_schedule
    maximum = len(teaching_schedule(LessonBlueprint.model_validate_json(blueprint.plan_json))) if blueprint else 24
    for number in sorted({effective} | {step.step_no for step in steps if effective <= step.step_no <= effective+1}):
        if number > maximum:
            continue
        created = queue_segment(teaching, sess, conv, number, version, blueprint)
        step = next((step for step in steps if step.step_no == number), None)
        if step:
            step.source_task_id = created["task_id"]
    if (point and point.step_no >= effective) or not point:
        sess.add(PlaybackCheckpointRow(checkpoint_id="chk_"+uuid.uuid4().hex[:12],
            conversation_id=conv.conversation_id, generation=conv.generation, client_seq=(point.client_seq if point else 0)+1,
            step_no=effective, position_ms=0, board_state_json=json.dumps({"presentation":{
                "visible_chars":0,"text_complete":False,"paused":True,"audio_id":"","playback_version":version}})))
    return effective


def queue_segment(teaching, sess, conv, number, version, blueprint=None):
    resume_key = f":resume:{conv.generation}" if conv.generation > 1 else ""
    created = teaching.tasks.create_task(owner_user_id=conv.owner_user_id, kind="lesson_step_generate",
        subject_id=f"{conv.conversation_id}:{number}:1",
        idempotency_key=f"step:{conv.conversation_id}:{number}:1{resume_key}:mentor:{version}",
        stage="generate", lane="teaching_prefetch", deadline_seconds=180, session=sess)
    if created["deduped"]:
        return created
    teaching.authorization.bind_task(sess, conv, created["task_id"])
    source = blueprint.source_task_id if blueprint else None
    for name in ("teaching_step_v1", "knowledge_recap_v1"):
        saved = sess.scalar(select(PromptBindingRow).where(PromptBindingRow.task_id == source,
            PromptBindingRow.prompt_name == name)) if source else None
        if saved:
            values = {column.name:getattr(saved,column.name) for column in saved.__table__.columns
                if column.name not in ("binding_id","task_id","created_at")}
        else:
            try:
                template = teaching.prompts.load_local_template(name)
            except PromptUnavailable:
                if name == "teaching_step_v1":
                    raise
                continue
            values = {"prompt_name":name,"source":"local_fallback","template_snapshot":template["template"],
                "template_sha256":teaching.prompts._sha256(template["template"])}
        sess.add(PromptBindingRow(binding_id="pb_"+uuid.uuid4().hex[:12], task_id=created["task_id"], **values))
    return created

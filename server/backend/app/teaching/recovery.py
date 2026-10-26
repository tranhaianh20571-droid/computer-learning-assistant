"""Bounded, durable retries for observed teaching sessions without new grants."""
from datetime import timedelta, timezone
from time import monotonic

from sqlalchemy import or_, select

from ..logging_audit.models import TaskStateRow
from ..planning.models import utcnow
from ..tasks.service import LearningTaskRow
from .models import TeachingStepRow, AudioAssetRow, PlaybackCheckpointRow

RETRY_CODES = frozenset({
    "MODEL_TIMEOUT", "MODEL_CONNECTION_ERROR", "MODEL_PROVIDER_ERROR", "MODEL_RATE_LIMITED",
    "TTS_TIMEOUT", "TASK_EXPIRED", "TASK_LEASE_EXPIRED", "LEASE_LOST", "TASK_STORAGE_BUSY",
})
MAX_ATTEMPTS = 6


def recover_observed(background):
    from .service import MAX_STEPS_PER_LESSON
    service = background.service
    from .preparation_models import PreparationItemRow
    observed = [ident for ident, last in list(service.observed_sessions.items()) if monotonic() - last <= 90]
    if not observed:
        return
    with service.db.session() as session:
        subjects = []
        for ident in observed:
            checkpoint = session.scalar(select(PlaybackCheckpointRow).where(
                PlaybackCheckpointRow.conversation_id == ident).order_by(PlaybackCheckpointRow.client_seq.desc()))
            from .learning_models import LessonBlueprintRow
            from ..planning.models import load_json_field
            blueprint = session.get(LessonBlueprintRow, ident)
            cap = 240 if blueprint and load_json_field(blueprint.plan_json, {}).get("kind") == "source_reading" else MAX_STEPS_PER_LESSON
            current = max(1, checkpoint.step_no if checkpoint else 1)
            subjects.extend(LearningTaskRow.subject_id.startswith(f"{ident}:{number}:", autoescape=True)
                for number in range(current, cap + 1))
        ids = list(session.scalars(select(LearningTaskRow.task_id).join(TaskStateRow,
            TaskStateRow.task_id == LearningTaskRow.task_id).where(
                or_(*subjects),
                LearningTaskRow.kind.in_(("lesson_step_generate", "tts")),
                LearningTaskRow.status.in_(("failed", "expired")),
                LearningTaskRow.cancel_requested.is_(False),
                LearningTaskRow.attempt < MAX_ATTEMPTS, LearningTaskRow.generation < MAX_ATTEMPTS,
                ~select(PreparationItemRow.item_id).where(
                    PreparationItemRow.task_id==LearningTaskRow.task_id).exists() if getattr(service,"course_preparation",None) and service.course_preparation.enabled else True,
                TaskStateRow.error_code.in_(RETRY_CODES | {"OUTPUT_INVALID"}),
            ).order_by(LearningTaskRow.updated_at).limit(32)))
    for ident in ids:
        with service.db.session() as session:
            task = session.get(LearningTaskRow, ident, with_for_update=True)
            if (not task or task.status not in ("failed", "expired") or task.cancel_requested
                    or max(task.attempt, task.generation) >= MAX_ATTEMPTS
                    or task.idempotency_key.startswith("artifact-repair:")):
                continue
            event = session.get(TaskStateRow, ident)
            if not event or event.error_code not in RETRY_CODES | {"OUTPUT_INVALID"}:
                continue
            conv_id = task.subject_id.split(":")[0]
            if event.error_code == "OUTPUT_INVALID":
                from ..planning.models import ConversationRow

                conv = session.get(ConversationRow, conv_id)
                cached = (service.preparation.for_conversation(conv, 1)
                          if conv and task.kind == "lesson_step_generate"
                          and task.subject_id.split(":")[1] == "1" else None)
                if (not cached or not cached.persistent) and (
                        task.kind != "lesson_step_generate"
                        or max(task.attempt, task.generation) >= 3):
                    continue
            if monotonic() - service.observed_sessions.get(conv_id, 0) > 90:
                continue
            updated = task.updated_at
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            delay = min(60, 2 ** min(max(task.attempt, task.generation), 6))
            if utcnow() < updated + timedelta(seconds=delay):
                continue
            part = task.subject_id.split(":")[1]
            if not part.isdigit():
                continue
            step = session.scalar(select(TeachingStepRow).where(
                TeachingStepRow.conversation_id == conv_id, TeachingStepRow.generation == 1,
                TeachingStepRow.step_no == int(part)))
            if step and step.status == "ready" and step.text:
                if task.kind == "lesson_step_generate":
                    continue
                audio = session.get(AudioAssetRow, step.audio_id) if step.audio_id else None
                if audio and audio.status == "ready" and audio.text_version_no == step.text_version_no:
                    continue
            if not background.eligible(session, task):
                continue
            task.generation += 1
            task.status = "queued"
            task.lease_token = task.lease_owner = task.lease_until = None
            task.deadline_at = utcnow() + timedelta(seconds=180)
            task.updated_at = utcnow()
            service.events.record(ident, stage=task.stage, status="started",
                generation=task.generation, attempt=task.attempt, session=session)
            task.revision = session.get(TaskStateRow, ident).revision

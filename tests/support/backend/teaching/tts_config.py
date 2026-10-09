"""Owner-scoped saved TTS configuration and immutable task binding."""
import httpx
from ..slice1.adapters import build_adapter
from ..slice1.errors import error
from ..tasks.service import LearningTaskRow


def configured_tts_factory(configs, disclosures, fallback=None):
    def describe(owner):
        view=configs.latest_active_for_owner(owner,"tts")
        return view if view and view["protocol"]=="minimax" else None

    def factory(owner,task_id):
        view=describe(owner)
        if not view:return fallback(owner,task_id) if fallback else None
        from sqlalchemy import select
        from ..tasks.service import TaskService, Lease
        from ..logging_audit.repositories import PgTaskEventStore
        tasks=TaskService(configs.db,PgTaskEventStore(configs.db))
        with configs.db.session() as sess:
            task=sess.get(LearningTaskRow,task_id)
            if not task or task.owner_user_id!=owner:raise error("config_not_found")
            lease=Lease(task.task_id,task.attempt_id,task.lease_token,task.lease_owner,
                task.generation,task.attempt,task.lease_until,task.deadline_at,owner)
            tasks.validate_lease(lease,actor_id=owner,session=sess)
            if task.model_config_version!=f"{view['config_id']}:{view['config_version']}":
                raise error("config_not_found")
            if task.kind=="tts":
                from .models import TeachingStepRow
                conv_id,step_no,generation=task.subject_id.split(":")
                step=sess.scalar(select(TeachingStepRow).where(TeachingStepRow.conversation_id==conv_id,
                    TeachingStepRow.step_no==int(step_no),TeachingStepRow.generation==int(generation)))
                if not step:raise error("config_not_found")
                from ..planning.models import ConversationRow
                from .authorization import audio_scope
                conv=sess.get(ConversationRow,conv_id,populate_existing=True)
                if not conv or conv.deleted_at or conv.owner_user_id!=owner:
                    raise error("config_not_found")
                scope=audio_scope(conv)
                expected_text=step.text
            elif task.kind=="lesson_first_prepare":
                from .preparation import PreparedFirstSegmentRow
                row=sess.scalar(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.task_id==task_id))
                if not row:raise error("config_not_found")
                from .outputs import StepCandidate
                expected_text=StepCandidate.model_validate_json(row.candidate_json).text
                import json
                scope={"course_id":row.course_id,"lesson_id":row.lesson_id,
                    "outline_version_id":json.loads(row.context_json)["outline_version_id"]}
            else:raise error("config_not_found")
        verified_grant_id = None
        def authorize(text):
            nonlocal verified_grant_id
            if text!=expected_text:raise error("disclosure_required")
            authorize_task = getattr(factory, "authorize_task", None)
            if authorize_task:authorize_task(task_id,audio=True)
            with configs.db.session() as sess:
                tasks.validate_lease(lease,actor_id=owner,session=sess)
                from ..planning.models import ConversationRow,CourseRow
                if task.kind=="tts":
                    live=sess.get(ConversationRow,conv_id,populate_existing=True)
                    current=sess.get(TeachingStepRow,step.step_id,populate_existing=True)
                    course=sess.get(CourseRow,live.course_id,populate_existing=True) if live else None
                    if (not live or live.deleted_at or live.owner_user_id!=owner or live.session_state!="active"
                        or not course or course.deleted_at or course.current_outline_version_id!=live.outline_version_id
                        or not current or current.text_version_no!=step.text_version_no or current.text!=expected_text):
                        raise error("disclosure_required")
                else:
                    live=sess.get(PreparedFirstSegmentRow,row.cache_id,populate_existing=True)
                    if not live or live.task_id!=task_id or live.context_json!=row.context_json or live.candidate_json!=row.candidate_json:
                        raise error("disclosure_required")
            current=configs.get(view["config_id"],actor_id=owner,is_admin=False)
            if not current["is_active"] or current["config_version"]!=view["config_version"]:
                raise error("config_not_found")
            verified = disclosures.verify_or_audit(task_id=task_id,config_id=view["config_id"],
                content_category="audio_text",actor_id=owner,scope_snapshot=scope,
                expected_grant_id=verified_grant_id)
            verified_grant_id = verified["grant_id"]
        authorize(expected_text)
        credentials=configs.resolve_credentials(view["config_id"])
        if not credentials.get("api_key") or not credentials.get("voice_id"):
            raise error("credential_invalid")
        client=httpx.Client(timeout=60,trust_env=False,follow_redirects=False)
        try:
            adapter=build_adapter("minimax",endpoint=view["endpoint"],model_name=view["model_name"] or "speech-2.8-hd",
                credentials=credentials,voice_id=credentials["voice_id"],client=client)
        except Exception:
            client.close();raise
        adapter._owns_client=True
        synthesize=adapter.synthesize
        def authorized_synthesize(text,**kwargs):
            authorize(text)
            return synthesize(text,**kwargs)
        adapter.synthesize=authorized_synthesize
        adapter.config_id=view["config_id"];adapter.config_version=view["config_version"];adapter.task_id=task_id
        return adapter

    factory.config_for_owner=describe
    factory.disclosures=disclosures
    factory.uses_saved_config=True
    factory.has_fallback=bool(fallback)
    return factory

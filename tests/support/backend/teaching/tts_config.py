"""Owner-scoped saved TTS configuration and immutable task binding."""
import httpx
import hashlib
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
            if task.kind in ("tts", "tts_answer"):
                from .models import TeachingStepRow
                conv_id,step_no,generation=task.subject_id.split(":")
                if task.kind == "tts":
                    step=sess.scalar(select(TeachingStepRow).where(TeachingStepRow.conversation_id==conv_id,
                        TeachingStepRow.step_no==int(step_no),TeachingStepRow.generation==int(generation)))
                    if not step:raise error("config_not_found")
                from ..planning.models import ConversationRow
                from .authorization import audio_scope
                conv=sess.get(ConversationRow,conv_id,populate_existing=True)
                if not conv or conv.deleted_at or conv.owner_user_id!=owner:
                    raise error("config_not_found")
                scope=audio_scope(conv)
                if task.kind == "tts_answer":
                    from ..planning.models import MessageRow
                    answer=sess.scalar(select(MessageRow).where(MessageRow.conversation_id==conv_id,
                        MessageRow.generation==int(generation),MessageRow.role=="assistant").order_by(MessageRow.created_at.desc()))
                    if not answer:raise error("config_not_found")
                    expected_text=answer.content_ref
                else:
                    expected_text=step.text
            elif task.kind=="feynman_audio":
                from .learning_models import FeynmanSessionRow, FeynmanTurnRow
                from ..planning.models import ConversationRow
                from .authorization import audio_scope
                conv_id, _, session_id, turn_id, _ = task.subject_id.split(":")
                quiz = sess.get(FeynmanSessionRow, session_id)
                quiz_turn = sess.get(FeynmanTurnRow, turn_id)
                conv = sess.get(ConversationRow, conv_id)
                if (not quiz or not quiz_turn or not conv or conv.deleted_at or quiz.state != "audio_pending"
                    or quiz.owner_user_id != owner or quiz.conversation_id != conv_id
                    or quiz_turn.session_id != session_id or quiz_turn.audio_task_id != task_id):
                    raise error("disclosure_required")
                expected_text = quiz_turn.public_text
                scope = audio_scope(conv)
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
                if task.kind in ("tts", "tts_answer"):
                    live=sess.get(ConversationRow,conv_id,populate_existing=True)
                    current=sess.get(MessageRow,answer.message_id,populate_existing=True) if task.kind == "tts_answer" else sess.get(TeachingStepRow,step.step_id,populate_existing=True)
                    course=sess.get(CourseRow,live.course_id,populate_existing=True) if live else None
                    valid_state = "awaiting_resume" if task.kind == "tts_answer" else "active"
                    text_valid = current and current.content_ref == expected_text if task.kind == "tts_answer" else current and current.text_version_no==step.text_version_no and current.text==expected_text
                    if (not live or live.deleted_at or live.owner_user_id!=owner or live.session_state!=valid_state
                        or not course or course.deleted_at or course.current_outline_version_id!=live.outline_version_id
                        or not text_valid or (task.kind == "tts_answer" and live.generation != int(generation))):
                        raise error("disclosure_required")
                elif task.kind=="feynman_audio":
                    from .learning_models import FeynmanSessionRow, FeynmanTurnRow
                    from ..planning.models import CourseRow
                    live=sess.get(FeynmanSessionRow,session_id,populate_existing=True)
                    current=sess.get(FeynmanTurnRow,turn_id,populate_existing=True)
                    live_conv=sess.get(ConversationRow,conv_id,populate_existing=True)
                    course=sess.get(CourseRow,live_conv.course_id) if live_conv else None
                    if (not live or live.state!="audio_pending" or not current or current.public_text!=expected_text
                        or not live_conv or live_conv.deleted_at or not course or course.deleted_at
                        or course.current_outline_version_id!=live_conv.outline_version_id):
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
        character = None
        if task.kind in ("tts", "tts_answer", "feynman_audio"):
            from ..characters.service import segment_character
            with configs.db.session() as sess:
                if task.kind == "feynman_audio":
                    character = segment_character(sess, conv_id, quiz.after_step_no)
                elif task.kind == "tts_answer":
                    import json
                    point=json.loads(conv.restore_point_json or "{}")
                    character = segment_character(sess, conv_id, int(point.get("step_no", 1)))
                else:
                    character = segment_character(sess, conv_id, int(step_no))
        profile = character["profile"] if character else None
        voice_id = profile["voice_id"] if profile and profile.get("voice_id") else credentials["voice_id"]
        if profile and profile.get("voice_id") and profile.get("voice_config_id") != view["config_id"]:
            client.close()
            raise error("disclosure_required")
        from ..characters.voice_policy import profile_gender, resolve_voice
        if profile and profile_gender(profile) != "unknown":
            try:
                voice_id = resolve_voice(profile, adapter.list_voices(), profile.get("voice_id", ""))
                if not voice_id:
                    raise error("capability_test_failed")
            except Exception:
                adapter.close()
                raise
        from ..characters.speech import compile_speech, synthesize_plan, map_subtitles
        expression = None
        if character:
            import json
            from ..characters.models import CharacterSegmentRow
            from ..characters.expression import CharacterExpressionPlan
            with configs.db.session() as sess:
                number = quiz.after_step_no if task.kind == "feynman_audio" else int(point.get("step_no",1)) if task.kind == "tts_answer" else int(step_no)
                segment = sess.get(CharacterSegmentRow, (conv_id, number))
                frozen = json.loads(segment.expression_json or "{}").get("content") if segment else None
                if frozen:
                    expression = CharacterExpressionPlan.model_validate(frozen)
        event = "explaining"
        if task.kind == "feynman_audio":
            import json
            from ..characters.delivery import feedback_event
            evaluation = json.loads(quiz_turn.evaluation_json or "{}")
            event = feedback_event(evaluation, help_requested=quiz_turn.kind == "help") if (
                evaluation or quiz_turn.kind == "help") else "question"
        speech_plan = compile_speech(expected_text, profile, getattr(adapter, "model_name", view["model_name"]),
            voice_id, expression=expression, event=event)
        if speech_plan and task.kind == "tts":
            import json
            from dataclasses import asdict
            from ..characters.speech import SpeechPlan, VERSION
            from ..characters.models import CharacterSegmentRow
            with configs.db.session() as sess:
                tasks.validate_lease(lease, actor_id=owner, session=sess)
                segment = sess.get(CharacterSegmentRow, (conv_id, int(step_no)), with_for_update=True)
                if segment:
                    snapshot = json.loads(segment.expression_json or "{}")
                    source = hashlib.sha256(json.dumps([expected_text, view["model_name"],
                        view["config_id"], view["config_version"], voice_id]).encode()).hexdigest()
                    if snapshot.get("speech_source") == source and snapshot.get("speech"):
                        data = snapshot["speech"]
                        speech_plan = SpeechPlan(**{**data, "spans": tuple(tuple(span) for span in data["spans"]),
                            "voice_modify": tuple(tuple(pair) for pair in data.get("voice_modify", [])),
                            "pronunciation": tuple(data.get("pronunciation", [])),
                            "capabilities": tuple(data.get("capabilities", [])),
                            "segments": tuple(tuple(part) for part in data.get("segments", []))})
                    else:
                        snapshot.update(speech_source=source, speech=asdict(speech_plan), compiler_version=VERSION)
                        segment.expression_json = json.dumps(snapshot, ensure_ascii=False)
                    segment.speech_fingerprint = speech_plan.fingerprint
        def authorized_synthesize(text,**kwargs):
            authorize(text)
            if not profile and speech_plan.speech_text == text:
                result = synthesize(text, **kwargs)
                result["subtitle"] = map_subtitles(speech_plan, result.get("subtitle"))
                result["speech_fingerprint"] = speech_plan.fingerprint
                return result
            if speech_plan:
                from ..characters.models import CharacterCardRow
                if character:
                    with configs.db.session() as sess:
                        card=sess.get(CharacterCardRow, character["card_id"])
                        if not card or card.deleted_at:raise error("disclosure_revoked")
                # Only this frozen compiler result may differ from the approved display text.
                result = synthesize_plan(speech_plan, profile, getattr(adapter, "model_name", view["model_name"]),
                    voice_id, synthesize, authorize)
                result["text_sha256"] = hashlib.sha256(expected_text.encode("utf8")).hexdigest()
                result["speech_fingerprint"] = speech_plan.fingerprint
                return result
            return synthesize(text,**kwargs)
        adapter.synthesize=authorized_synthesize
        adapter.config_id=view["config_id"];adapter.config_version=view["config_version"];adapter.task_id=task_id
        return adapter

    factory.config_for_owner=describe
    factory.disclosures=disclosures
    factory.uses_saved_config=True
    factory.has_fallback=bool(fallback)
    return factory

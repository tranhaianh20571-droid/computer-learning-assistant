"""Prepare one owned upcoming first segment, without starting learning."""
import hashlib
import json
import os
import uuid
from datetime import timedelta,timezone
from time import monotonic
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor,TimeoutError as FutureTimeout
from sqlalchemy import select,func,text
from ..planning.models import CourseRow,CourseUnitRow,LessonRow,LessonProgressRow,LessonContentVersionRow,ConversationRow,utcnow
from ..tasks.service import LearningTaskRow
from ..slice1.errors import error as config_error
from .learning_models import PreparedFirstSegmentRow,PreparedLessonPlanRow,LessonBlueprintRow
from .learning_outputs import LessonBlueprint,teaching_schedule
from .pedagogy import lesson_context,plan_key,prepared_plan,text_context
from .outputs import StepCandidate,declaration_to_scene,validate_step_duration
from .models import AudioAssetRow,TeachingStepRow


def setting(name,default):
    try:return max(0,int(os.environ.get(name,str(default))))
    except ValueError:return default


def fresh(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


class LessonPreparation:
    def __init__(self,teaching):
        self.teaching=teaching
        self.daily_limit=setting('FIRST_PREWARM_DAILY_LIMIT',20)
        self.max_courses=setting('FIRST_PREWARM_MAX_COURSES',100)
        self.max_bytes=setting('FIRST_PREWARM_MAX_BYTES',268435456)
        self.ttl_days=setting('FIRST_PREWARM_TTL_DAYS',7)
        self.queue_limit=setting('FIRST_PREWARM_QUEUE_LIMIT',100)
        self.timeout=setting('FIRST_PREWARM_MODEL_TIMEOUT',180)
        self.enabled=bool(setting('FIRST_PREWARM_ENABLED',1))
        self._gc_cursor=''
        self._reconcile_cursor=''
        self.max_entries=setting('FIRST_PREWARM_MAX_ENTRIES',50)

    def _model(self,owner):
        configs=self.teaching.configs
        if configs:
            from ..agents.model_routing import role_config
            return role_config(configs,owner,"teaching")
        factory=self.teaching.model_adapter_factory
        getter=getattr(factory,'config_for_owner',None)
        return getter(owner) if getter else {'config_id':'injected','config_version':1} if factory else None

    def _audio(self,owner):
        getter=getattr(self.teaching.tts_adapter_factory,'config_for_owner',None)
        return getter(owner) if getter else None

    def keys(self,conv,context=None):
        context=context or lesson_context(self.teaching,conv)
        with self.teaching.db.session() as sess:
            version=sess.scalars(select(LessonContentVersionRow).where(LessonContentVersionRow.lesson_id==conv.lesson_id)
                .order_by(LessonContentVersionRow.content_version_no.desc())).first()
            content_version=version.content_version_no if version else 1
            from ..characters.service import segment_character,default_mentor_snapshot
            character=segment_character(sess,conv.conversation_id,1)
            if not sess.get(ConversationRow,conv.conversation_id):
                character=default_mentor_snapshot(sess,conv.owner_user_id)
        if character:context={**context,"_prepared_character":character}
        model=self._model(conv.owner_user_id)
        if not model:return None,None,context
        context={k:v for k,v in context.items() if k != 'source_reading_plan'}
        teacher=self.teaching.prompts.load_local_template('teaching_step_v1')
        alignment=self.teaching.prompts.load_local_template('board_alignment_v1')
        key=digest({'owner':conv.owner_user_id,'context':text_context(context),'content_version':content_version,
            'character':text_context({"prepared_character":character}) if character else None,
            'plan_key':plan_key(self.teaching,conv,context,include_memory=False),'teacher':teacher['sha256'],
            'model':[model['config_id'],model['config_version']],'contract':7 if context.get('teaching_mode') == 'source_reading' else 5,
            'alignment':self.teaching.prompts._sha256(alignment['template'])})
        voice=self._audio(conv.owner_user_id)
        audio_key=digest([key,voice['config_id'],voice['config_version'],character]) if voice else ''
        return key,audio_key,context

    def _lessons(self,sess,course):
        return list(sess.scalars(select(LessonRow).join(CourseUnitRow,LessonRow.unit_id==CourseUnitRow.unit_id)
            .where(LessonRow.outline_version_id==course.current_outline_version_id)
            .order_by(CourseUnitRow.order_index,LessonRow.order_index)))

    def _target(self,sess,course,owner,after_lesson_id=None):
        lessons=self._lessons(sess,course)
        done=set(sess.scalars(select(LessonProgressRow.lesson_id).where(LessonProgressRow.owner_user_id==owner,
            LessonProgressRow.learning_state.in_(['user_completed','completed']))))
        if after_lesson_id is None and self.teaching.authorization.automatic_enabled:
            current=sess.scalars(select(ConversationRow).where(ConversationRow.course_id==course.course_id,
                ConversationRow.owner_user_id==owner,ConversationRow.outline_version_id==course.current_outline_version_id,
                ConversationRow.kind=='teaching',ConversationRow.deleted_at.is_(None),
                ConversationRow.session_state.in_(['active','paused','awaiting_resume']))
                .order_by(ConversationRow.created_at.desc())).first()
            if current and self.teaching._step_artifact(sess,current,1)[2]:
                after_lesson_id=current.lesson_id
        if after_lesson_id is not None:
            index=next((i for i,item in enumerate(lessons) if item.lesson_id==after_lesson_id),None)
            if index is None:return None
            lessons=lessons[index+1:]
        return next((item for item in lessons if item.lesson_id not in done),None)

    def prepare_next(self,conversation_id,owner):
        if not self.teaching.authorization.automatic_enabled:return {'status':'disabled'}
        with self.teaching.db.session() as sess:
            conv=sess.get(ConversationRow,conversation_id)
            if not conv or conv.owner_user_id!=owner or conv.deleted_at or conv.session_state!='active':
                return {'status':'unavailable'}
            course=sess.get(CourseRow,conv.course_id)
            if not course or course.deleted_at or course.current_outline_version_id!=conv.outline_version_id:
                return {'status':'unavailable'}
            if not self.teaching._step_artifact(sess,conv,1)[2]:return {'status':'deferred'}
            course_id,lesson_id=conv.course_id,conv.lesson_id
        return self.prepare_course(course_id,owner,after_lesson_id=lesson_id)

    def reconcile(self,limit=4):
        """Bounded server-owned discovery; never starts a learning conversation."""
        if self.teaching.course_preparation.enabled:
            return self.teaching.course_preparation.reconcile(limit)
        if not self.enabled or not self.teaching.authorization.automatic_enabled:return []
        with self.teaching.db.session() as sess:
            query=select(CourseRow.course_id,CourseRow.owner_user_id,CourseRow.created_at).where(CourseRow.deleted_at.is_(None),
                CourseRow.current_outline_version_id.is_not(None))
            if getattr(self.teaching.configs,'db',None) is not None:
                from ..slice1.models import ServiceConfigRow
                configured=select(ServiceConfigRow.config_id).where(ServiceConfigRow.owner_user_id==CourseRow.owner_user_id,
                    ServiceConfigRow.owner_scope=='user',ServiceConfigRow.kind=='content_model',ServiceConfigRow.is_active.is_(True),
                    ServiceConfigRow.encrypted_credentials!='')
                for suffix in ('test','invalid'):
                    configured=configured.where(ServiceConfigRow.endpoint.not_like('%://%.'+suffix),
                        ServiceConfigRow.endpoint.not_like('%://%.'+suffix+'/%'))
                query=query.where(configured.exists())
            query=query.order_by(CourseRow.created_at.desc(),CourseRow.course_id)
            if self._reconcile_cursor:
                stamp,ident=self._reconcile_cursor
                query=query.where((CourseRow.created_at<stamp)|((CourseRow.created_at==stamp)&(CourseRow.course_id>ident)))
            rows=list(sess.execute(query.limit(max(1,limit))))
            if not rows:
                self._reconcile_cursor=''
                return []
            self._reconcile_cursor=(rows[-1][2],rows[-1][0])
        outcomes=[]
        for course_id,owner,_ in rows:
            if not self._model(owner):continue
            outcomes.append(dict(course_id=course_id,**self.prepare_course(course_id,owner)))
        return outcomes

    def prepare_course(self,course_id,owner,*,_confirmation=None,after_lesson_id=None,
                       lesson_id=None,persistent=False):
        service=self.teaching
        if not self.enabled:return {'status':'disabled'}
        garbage=[]
        with service.db.session() as sess:
            course=sess.get(CourseRow,course_id,with_for_update=True)
            if not course or course.owner_user_id!=owner or course.deleted_at or not course.current_outline_version_id:
                return {'status':'unavailable'}
            target=sess.get(LessonRow,lesson_id) if lesson_id else self._target(sess,course,owner,after_lesson_id)
            if target and target.outline_version_id != course.current_outline_version_id:
                return {'status':'unavailable'}
            if not target:return {'status':'completed'}
            if self.teaching.course_preparation.enabled and not persistent:
                self.teaching.course_preparation.promote(sess,course,target)
                return {'status':'deferred','lesson_id':target.lesson_id}
            if persistent and getattr(service,"assessments",None):
                scope=service.assessments.scopes.get(course_id,target.unit_id,owner)
                if scope["status"]!="succeeded":return {'status':'deferred','lesson_id':target.lesson_id}
            conv=SimpleNamespace(course_id=course_id,owner_user_id=owner,lesson_id=target.lesson_id,
                outline_version_id=course.current_outline_version_id,conversation_id='prewarm')
            automatic = not _confirmation and service.authorization.automatic_enabled
            confirmation=service.authorization.prepare_confirmation(sess,course_id,target.lesson_id,owner,
                **_confirmation) if _confirmation else None
            from ..slice1.errors import Slice1Error
            if automatic:
                try:
                    request = service.authorization.automatic_request(course_id,target.lesson_id,
                        actor_id=owner,purpose='prewarm',session=sess)
                    confirmation = service.authorization.prepare_confirmation(sess,course_id,target.lesson_id,owner,**request)
                except Slice1Error as exc:
                    return {'status':'unconfigured' if exc.code=='config_not_found' else 'authorization_required',
                        'lesson_id':target.lesson_id,'error_code':exc.code}
            if not confirmation:
                try:service.authorization.require(sess,conv,prewarm=True)
                except Slice1Error as exc:
                    return {'status':'authorization_required','lesson_id':target.lesson_id,'error_code':exc.code}
            key,audio_key,context=self.keys(conv)
            if not key:return {'status':'unconfigured','lesson_id':target.lesson_id}
            if confirmation and not confirmation['tts_confirmed']:audio_key=''
            elif not confirmation and service.authorization.enforced:
                try:service.authorization.require(sess,conv,audio=True,prewarm=True)
                except Slice1Error:audio_key=''
            row=sess.scalars(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.owner_user_id==owner,
                PreparedFirstSegmentRow.course_id==course_id,PreparedFirstSegmentRow.lesson_id==target.lesson_id,
                PreparedFirstSegmentRow.text_key==key,PreparedFirstSegmentRow.audio_key==audio_key).with_for_update()).first()
            if row is None and automatic:
                current=sess.scalars(select(ConversationRow).where(ConversationRow.owner_user_id==owner,
                    ConversationRow.course_id==course_id,ConversationRow.lesson_id==target.lesson_id,
                    ConversationRow.deleted_at.is_(None),ConversationRow.session_state=='active')
                    .order_by(ConversationRow.created_at.desc())).first()
                pending=sess.scalar(select(LearningTaskRow.task_id).where(
                    LearningTaskRow.subject_id.like(current.conversation_id+':1:%'),
                    LearningTaskRow.kind=='lesson_step_generate',LearningTaskRow.status.in_(['queued','leased','running']),
                    LearningTaskRow.deadline_at>utcnow())) if current else None
                if pending:return {'status':'deferred','lesson_id':target.lesson_id}
            if row and row.consumed_conversation_id:
                previous=sess.get(ConversationRow,row.consumed_conversation_id)
                if previous and not previous.deleted_at and previous.session_state in ('active','paused','awaiting_resume'):
                    return {'status':'consumed','lesson_id':target.lesson_id}
            if row and row.status in ('queued','generating'):
                pending=sess.get(LearningTaskRow,row.task_id)
                if not pending or fresh(pending.deadline_at)<=utcnow() or pending.status in ('failed','expired','cancelled'):
                    row.status='failed';row.error_code=row.error_code or 'WORKER_RECOVERY_FAILED'
                elif pending.status=='succeeded' and row.candidate_json=='{}':
                    row.status='failed';row.error_code='WORKER_RECOVERY_FAILED'
            if row and row.lesson_id==target.lesson_id and row.text_key==key and (row.persistent or fresh(row.expires_at)>utcnow()):
                if row.status=='failed' and row.audio_key==audio_key:
                    if not confirmation:
                        return {'status':'failed','lesson_id':target.lesson_id,'error_code':row.error_code}
                    if automatic and not persistent:
                        attempts=sess.scalar(select(func.count()).select_from(LearningTaskRow).where(
                            LearningTaskRow.kind=='lesson_first_prepare',LearningTaskRow.owner_user_id==owner,
                            LearningTaskRow.subject_id=='first:'+key[:32]))
                        previous_task=sess.get(LearningTaskRow,row.task_id)
                        if attempts>=2 or previous_task and fresh(previous_task.updated_at)+timedelta(seconds=60)>utcnow():
                            return {'status':'failed','lesson_id':target.lesson_id,'error_code':row.error_code}
                if row.consumed_conversation_id:
                    previous=sess.get(ConversationRow,row.consumed_conversation_id)
                    if previous and not previous.deleted_at:return {'status':'consumed','lesson_id':target.lesson_id}
                if row.status in ('queued','generating') or (row.status!='failed' and row.audio_key==audio_key):
                    if confirmation:
                        service.authorization.record(sess,confirmation,owner,row.task_id)
                    return {'status':row.status,'task_id':row.task_id,'lesson_id':target.lesson_id}
            if sess.bind.dialect.name=='postgresql':
                sess.execute(text('SELECT pg_advisory_xact_lock(:owner)'),{'owner':int(hashlib.sha256(owner.encode()).hexdigest()[:15],16)})
            now=utcnow();midnight=now.replace(hour=0,minute=0,second=0,microsecond=0)
            daily=sess.scalar(select(func.count()).select_from(LearningTaskRow).where(LearningTaskRow.kind=='lesson_first_prepare',
                LearningTaskRow.owner_user_id==owner,LearningTaskRow.created_at>=midnight))
            queued=sess.scalar(select(func.count()).select_from(LearningTaskRow).where(LearningTaskRow.kind=='lesson_first_prepare',
                LearningTaskRow.status.in_(['queued','leased','running']),LearningTaskRow.deadline_at>now))
            if (not persistent and daily>=self.daily_limit) or queued>=self.queue_limit:
                return {'status':'deferred','lesson_id':target.lesson_id}
            kept=list(sess.scalars(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.owner_user_id==owner,
                PreparedFirstSegmentRow.persistent.is_(False),
                PreparedFirstSegmentRow.consumed_conversation_id.is_(None)).order_by(PreparedFirstSegmentRow.created_at)))
            def size(item):return item.audio_bytes+len(item.candidate_json.encode())+len(item.plan_json.encode())+len(item.context_json.encode())
            while not persistent and kept and (len(kept)>=self.max_entries or
                    (row is None and course_id not in {item.course_id for item in kept} and len({item.course_id for item in kept})>=self.max_courses) or
                    sum(size(item) for item in kept)>=self.max_bytes):
                old=kept.pop(0)
                if row and old.cache_id==row.cache_id:continue
                if old.audio_object_key:garbage.append(old.audio_object_key)
                sess.delete(old)
            if not persistent and (not self.max_courses or not self.max_entries or not self.max_bytes):return {'status':'deferred','lesson_id':target.lesson_id}
            resource_reason=self.resource_wait_reason() if persistent else None
            if resource_reason:return {'status':'deferred','lesson_id':target.lesson_id,
                'error_code':'PREPARATION_'+resource_reason.upper()+'_WAIT'}
            text_source=row if row and row.candidate_json!='{}' and (row.persistent or fresh(row.expires_at)>now) else sess.scalars(
                select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.owner_user_id==owner,
                    PreparedFirstSegmentRow.course_id==course_id,PreparedFirstSegmentRow.lesson_id==target.lesson_id,
                    PreparedFirstSegmentRow.text_key==key,PreparedFirstSegmentRow.candidate_json!='{}',
                    PreparedFirstSegmentRow.status.in_(['ready','text_ready','consumed']),
                    (PreparedFirstSegmentRow.persistent.is_(True)) | (PreparedFirstSegmentRow.expires_at>now))
                .order_by(PreparedFirstSegmentRow.created_at.desc())).first()
            preserve=bool(text_source)
            if row and row.audio_object_key:garbage.append(row.audio_object_key)
            task=service.tasks.create_task(owner_user_id=owner,kind='lesson_first_prepare',subject_id='first:'+key[:32],
                idempotency_key='first:'+key+':'+uuid.uuid4().hex[:12],stage='tts' if preserve else 'generate',
                lane='maintenance',deadline_seconds=86400 if persistent else setting('FIRST_PREWARM_QUEUE_TIMEOUT',600),session=sess)
            if persistent:
                from ..agents.task_binding import AgentBudgetRow
                sess.get(AgentBudgetRow,task['task_id']).request_limit=8
            if confirmation:
                service.authorization.record(sess,confirmation,owner,task['task_id'])
            else:
                service.authorization.bind_task(sess,conv,task['task_id'],prewarm=True)
            if not row:
                row=PreparedFirstSegmentRow(cache_id='first_'+uuid.uuid4().hex[:12],owner_user_id=owner,course_id=course_id,
                    lesson_id=target.lesson_id,text_key=key,task_id=task['task_id'],context_json=json.dumps(context,ensure_ascii=False),
                    plan_json=prepared_plan(service,conv,context).model_dump_json(),expires_at=now+timedelta(days=self.ttl_days))
                sess.add(row)
            if preserve:
                preserved_context=json.loads(text_source.context_json)
                if context.get("_prepared_character"):
                    preserved_context["_prepared_character"]=context["_prepared_character"]
                row.context_json=json.dumps(preserved_context,ensure_ascii=False);row.plan_json=text_source.plan_json
                row.candidate_json=text_source.candidate_json
            else:
                row.context_json=json.dumps(context,ensure_ascii=False)
                row.plan_json=prepared_plan(service,conv,context).model_dump_json();row.candidate_json='{}'
            row.lesson_id=target.lesson_id;row.text_key=key;row.audio_key=audio_key;row.status='queued';row.task_id=task['task_id']
            row.persistent=persistent
            row.audio_object_key='';row.audio_json='{}';row.audio_bytes=0;row.error_code=None;row.consumed_conversation_id=None
            row.created_at=now;row.expires_at=now+timedelta(days=self.ttl_days)
            if service.authorization.enforced and not confirmation:
                from ..slice1.errors import Slice1Error
                try:service.authorization.bind_task(sess,conv,task['task_id'],audio=True,prewarm=True)
                except Slice1Error:
                    row.audio_key=''
            elif confirmation and not confirmation['tts_confirmed']:
                row.audio_key=''
            service.prompts.bind_for_task(task['task_id'],'teaching_step_v1',allow_fallback=True)
            service.prompts.bind_for_task(task['task_id'],'board_alignment_v1',allow_fallback=True)
            service.prompts.bind_for_task(task['task_id'],'lesson_blueprint_v1',allow_fallback=True)
            service.prompts.bind_for_task(task['task_id'],'knowledge_recap_v1',allow_fallback=True)
        for object_key in garbage:self._delete_unreferenced(object_key)
        return {'status':'queued','task_id':task['task_id'],'lesson_id':target.lesson_id}

    def prepare_owner(self,owner):
        with self.teaching.db.session() as sess:
            ids=list(sess.scalars(select(CourseRow.course_id).where(CourseRow.owner_user_id==owner,CourseRow.deleted_at.is_(None),
                CourseRow.current_outline_version_id.is_not(None)).order_by(CourseRow.created_at.desc()).limit(self.max_courses)))
        return {'data':[dict(course_id=ident,**self.prepare_course(ident,owner)) for ident in ids]}

    def status(self,course_id,owner):
        with self.teaching.db.session() as sess:
            course=sess.get(CourseRow,course_id)
            if not course or course.deleted_at or course.owner_user_id!=owner:
                from .errors import terr
                raise terr('course_not_found',status=404)
            target=self._target(sess,course,owner)
            if not self.enabled:return {'status':'disabled','lesson_id':target.lesson_id if target else None}
            if not target:return {'status':'completed','lesson_id':None}
            if not self._model(owner):return {'status':'unconfigured','lesson_id':target.lesson_id}
            if self.teaching.authorization.enforced:
                from ..slice1.errors import Slice1Error
                try:
                    if self.teaching.authorization.automatic_enabled:
                        self.teaching.authorization.automatic_request(course_id,target.lesson_id,actor_id=owner,purpose='prewarm',session=sess)
                    else:
                        self.teaching.authorization.require(sess,SimpleNamespace(owner_user_id=owner,course_id=course_id,
                            lesson_id=target.lesson_id,outline_version_id=course.current_outline_version_id,conversation_id=None),prewarm=True)
                except Slice1Error as exc:
                    return {'status':'authorization_required','lesson_id':target.lesson_id,'error_code':exc.code}
            view_conv=SimpleNamespace(owner_user_id=owner,course_id=course_id,lesson_id=target.lesson_id,
                outline_version_id=course.current_outline_version_id,conversation_id='prewarm')
            key,audio_key,_=self.keys(view_conv)
            row=sess.scalars(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.course_id==course_id,
                PreparedFirstSegmentRow.owner_user_id==owner,PreparedFirstSegmentRow.lesson_id==target.lesson_id,
                PreparedFirstSegmentRow.text_key==key).order_by((PreparedFirstSegmentRow.audio_key==audio_key).desc(),
                PreparedFirstSegmentRow.created_at.desc())).first()
            if not target:return {'status':'completed','lesson_id':None}
            if not row or row.lesson_id!=target.lesson_id or (not row.persistent and fresh(row.expires_at)<=utcnow()):return {'status':'pending','lesson_id':target.lesson_id}
            task=sess.get(LearningTaskRow,row.task_id)
            if row.status in ('queued','generating') and task and fresh(task.deadline_at)<=utcnow():
                return {'status':'failed','lesson_id':target.lesson_id,'error_code':'MODEL_TIMEOUT'}
            return {'status':row.status,'lesson_id':row.lesson_id,'audio_ready':bool(row.audio_object_key),'error_code':row.error_code}

    def claim_for_start(self,sess,course,lesson):
        conv=SimpleNamespace(owner_user_id=course.owner_user_id,course_id=course.course_id,lesson_id=lesson.lesson_id,
            outline_version_id=course.current_outline_version_id,conversation_id='prewarm')
        key,audio_key,_=self.keys(conv)
        row=sess.scalars(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.course_id==course.course_id,
            PreparedFirstSegmentRow.owner_user_id==course.owner_user_id,PreparedFirstSegmentRow.lesson_id==lesson.lesson_id,
            PreparedFirstSegmentRow.text_key==key,
            (PreparedFirstSegmentRow.persistent.is_(True)) | (PreparedFirstSegmentRow.expires_at>utcnow()),
            PreparedFirstSegmentRow.status.in_(['queued','generating','ready','text_ready','consumed']))
            .order_by((PreparedFirstSegmentRow.audio_key==audio_key).desc(),PreparedFirstSegmentRow.created_at.desc())
            .with_for_update()).first()
        return row

    def _row_for_conv(self,sess,conv):
        blueprint=sess.get(LessonBlueprintRow,conv.conversation_id)
        cache_id=json.loads(blueprint.context_json).get('_first_cache_id') if blueprint else None
        row=sess.get(PreparedFirstSegmentRow,cache_id) if cache_id else None
        if row and row.owner_user_id==conv.owner_user_id and row.lesson_id==conv.lesson_id and row.consumed_conversation_id==conv.conversation_id:
            return row
        return None

    def for_conversation(self,conv,step_no):
        if step_no!=1:return None
        with self.teaching.db.session() as sess:
            row=self._row_for_conv(sess,conv)
            if row and row.candidate_json!='{}' and row.status in ('ready','text_ready','consumed'):
                if (not row.persistent and fresh(row.expires_at)<=utcnow()) or self.keys(conv)[0]!=row.text_key:return None
                sess.expunge(row);return row
        return None

    def pending_audio_for(self,conv):
        with self.teaching.db.session() as sess:
            row=self._row_for_conv(sess,conv)
            if not row or not row.audio_key or row.audio_object_key or row.candidate_json=='{}':return False
            task=sess.get(LearningTaskRow,row.task_id)
            return bool(task and task.status in ('queued','leased','running') and fresh(task.deadline_at)>utcnow()
                and self.keys(conv)[1]==row.audio_key)

    def waiting(self,task):
        if not self.enabled:return False
        if task['kind']!='lesson_step_generate' or ':followup:' in task['subject_id'] or task['subject_id'].split(':')[1]!='1':return False
        with self.teaching.db.session() as sess:
            conv=sess.get(ConversationRow,task['subject_id'].split(':')[0])
            if not conv:return False
            row=self._row_for_conv(sess,conv)
            if not row or row.status not in ('queued','generating'):return False
            preparation=sess.get(LearningTaskRow,row.task_id)
            return bool(preparation and preparation.status in ('queued','leased','running') and fresh(preparation.deadline_at)>utcnow())

    def run(self,task_id):
        service=self.teaching;adapter=None;speech=None;object_key='';lease=None
        try:
            lease=service.tasks.claim(task_id,lease_owner='first-prewarm',lease_seconds=120,allow_reclaim_active=False)
            with service.db.session() as sess:
                row=sess.scalars(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.task_id==task_id)).first()
                if not row:raise config_error('config_not_found')
                if row.persistent:
                    task=sess.get(LearningTaskRow,task_id)
                    task.deadline_at=min(fresh(task.deadline_at),utcnow()+timedelta(seconds=600))
                row.status='generating';sess.flush();sess.expunge(row)
            service._check_authorization(task_id)
            stored_context=json.loads(row.context_json)
            context={key:value for key,value in stored_context.items() if not key.startswith('_')}
            plan=LessonBlueprint.model_validate_json(row.plan_json)
            conv=SimpleNamespace(owner_user_id=row.owner_user_id,course_id=row.course_id,lesson_id=row.lesson_id,
                outline_version_id=context['outline_version_id'],conversation_id=row.cache_id,
                prepared_character=stored_context.get("_prepared_character"),
                prepared_request_limit=8 if row.persistent else 2)
            if self.keys(conv)[0]!=row.text_key:raise config_error('config_not_found')
            from .source_reading import SourceReadingStepCandidate
            candidate_type = SourceReadingStepCandidate if context.get('teaching_mode') == 'source_reading' else StepCandidate
            candidate=candidate_type.model_validate_json(row.candidate_json) if row.candidate_json!='{}' else None
            if not candidate:
                from ..agents.task_binding import AgentBudgetRow
                with service.db.session() as sess:
                    service.tasks.validate_lease(lease,session=sess)
                    budget=sess.get(AgentBudgetRow,task_id,with_for_update=True)
                    budget.request_limit=min(budget.request_limit,8 if row.persistent else 2)
                adapter=service._model_adapter(row.owner_user_id,task_id)
                if not adapter:raise config_error('capability_unavailable')
                from dataclasses import replace
                from ..agents.runner import deps_for_source
                bound=deps_for_source(adapter,max_requests=conv.prepared_request_limit)
                original_check=bound.check_state
                began=monotonic()
                def check_prewarm():
                    if original_check:original_check()
                    service._check_authorization(task_id)
                    if monotonic()-began>=self.timeout:raise config_error('timeout')
                    with service.db.session() as sess:
                        current=sess.get(PreparedFirstSegmentRow,row.cache_id)
                        if not current or current.task_id!=task_id or self.keys(conv)[0]!=row.text_key:
                            raise config_error('config_not_found')
                adapter.agent_deps=replace(bound,check_state=check_prewarm,
                    output_retries=7 if row.persistent else bound.output_retries)
                adapter.agent_deps.budget.request_limit=min(bound.budget.request_limit,8 if row.persistent else 2)
                adapter.agent_deps.budget.wire_limit=min(bound.budget.wire_limit,8 if row.persistent else 2)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future=pool.submit(service._generate_one,conv,task_id,adapter,1,prepared=(plan,context))
                    while True:
                        try:plan,context,_,result=future.result(timeout=30);break
                        except FutureTimeout:service.tasks.heartbeat(lease,lease_seconds=120)
                candidate=result.payload.steps[0];validate_step_duration(candidate.text,
                    source_reading=context.get("teaching_mode") == "source_reading")
                with service.db.session() as sess:
                    current=sess.get(PreparedFirstSegmentRow,row.cache_id,with_for_update=True)
                    if not current or current.task_id!=task_id:raise config_error('config_not_found')
                    current.candidate_json=candidate.model_dump_json()
                    current.plan_json=plan.model_dump_json()
                    current.context_json=json.dumps({**stored_context,**context},ensure_ascii=False)
            # The complete validated text/board becomes independently consumable before speech.
            with service.db.session() as sess:
                service.tasks.validate_lease(lease,session=sess)
                service._check_authorization(task_id,session=sess)
                current=sess.get(PreparedFirstSegmentRow,row.cache_id,with_for_update=True)
                course=sess.get(CourseRow,row.course_id)
                if not current or current.task_id!=task_id or not course or course.deleted_at or self.keys(conv)[0]!=row.text_key:
                    raise config_error('config_not_found')
                current.candidate_json=candidate.model_dump_json()
                current.status='text_ready';current.error_code=None
            service.tasks.heartbeat(lease,lease_seconds=120)
            voice=self._audio(row.owner_user_id);audio_meta={};audio_bytes=None;audio_error=None
            if voice and self.keys(conv)[1]==row.audio_key:
                factory=service.tts_adapter_factory
                try:
                    service._check_authorization(task_id,audio=True)
                    with service.db.session() as sess:sess.get(LearningTaskRow,task_id).model_config_version=f"{voice['config_id']}:{voice['config_version']}"
                    speech=factory(row.owner_user_id,task_id)
                    for attempt in range(1 if row.persistent else 2):
                        try:
                            service._check_authorization(task_id,audio=True)
                            if row.persistent and not getattr(factory,"uses_saved_config",False):
                                from .course_preparation import debit_wire
                                with service.db.session() as sess:
                                    service.tasks.validate_lease(lease,session=sess)
                                    debit_wire(sess,sess.get(LearningTaskRow,task_id),config=voice)
                            audio_meta=speech.synthesize(candidate.text);break
                        except Exception as exc:
                            if row.persistent or attempt or getattr(exc,'code','') not in ('timeout','connection_error'):raise
                    cues=audio_meta.get('subtitle')
                    import math
                    if not cues or not audio_meta.get('audio_hex') or not all(isinstance(cue,dict) and isinstance(cue.get('begin_ms'),(int,float))
                        and isinstance(cue.get('end_ms'),(int,float)) and math.isfinite(cue['end_ms']) and 0<=cue['begin_ms']<cue['end_ms']<=120000 for cue in cues):
                        raise config_error('capability_unavailable')
                    if candidate.alignment_version == 1:
                        from .board_alignment import exact_subtitles
                        if not exact_subtitles(candidate.text, cues):
                            raise config_error('capability_unavailable')
                    audio_bytes=bytes.fromhex(audio_meta['audio_hex'])
                    object_key=f"prepared/{row.cache_id}/{task_id}.mp3";service.storage.put(object_key,audio_bytes)
                    audio_meta={'subtitle':cues,'audio_format':audio_meta.get('audio_format','mp3'),'duration_ms':int(max(cue['end_ms'] for cue in cues))}
                except Exception as exc:
                    if row.persistent:service.course_preparation.note_failure(task_id,exc,config=voice)
                    raw=getattr(exc,'code','tts_failed')
                    audio_error=({'timeout':'TTS_TIMEOUT','connection_error':'MODEL_CONNECTION_ERROR',
                        'tts_failed':'MODEL_PROVIDER_ERROR','rate_limited':'MODEL_RATE_LIMITED',
                        'quota_exceeded':'MODEL_QUOTA_EXCEEDED'}.get(raw,service._controlled_failure(exc))
                        if row.persistent else raw)
                    audio_meta={};audio_bytes=None
            def publish(sess):
                nonlocal audio_bytes,audio_meta,audio_error
                service._check_authorization(task_id,session=sess)
                current=sess.get(PreparedFirstSegmentRow,row.cache_id,with_for_update=True)
                course=sess.get(CourseRow,row.course_id)
                if not current or current.task_id!=task_id or not course or course.deleted_at or self.keys(conv)[0]!=row.text_key:
                    raise config_error('config_not_found')
                if audio_bytes:
                    from ..slice1.errors import Slice1Error
                    try:
                        service._check_authorization(task_id,audio=True,session=sess)
                        if self.keys(conv)[1]!=row.audio_key:raise config_error('config_not_found')
                        service.tts_adapter_factory.disclosures.verify_or_audit(task_id=task_id,config_id=voice['config_id'],
                            content_category='audio_text',actor_id=row.owner_user_id,session=sess,
                            scope_snapshot={'course_id':row.course_id,'lesson_id':row.lesson_id,'outline_version_id':json.loads(row.context_json)['outline_version_id']})
                    except Slice1Error as exc:
                        if row.persistent:raise
                        audio_error=service._controlled_failure(exc);audio_bytes=None;audio_meta={}
                current.candidate_json=candidate.model_dump_json();current.audio_json=json.dumps(audio_meta,ensure_ascii=False)
                current.audio_object_key=object_key if audio_bytes else '';current.audio_bytes=len(audio_bytes) if audio_bytes else 0
                current.status='consumed' if current.consumed_conversation_id else 'ready' if audio_bytes or not voice else 'text_ready'
                current.error_code=audio_error if row.persistent or audio_error in ('timeout','connection_error','credential_invalid','capability_unavailable',
                    'CONFIG_NOT_FOUND','DISCLOSURE_REQUIRED','DISCLOSURE_REVOKED','DISCLOSURE_EXPIRED') else None
            failed_audio=bool(row.persistent and audio_error)
            receipt=service.tasks.submit_result(lease,stage='tts' if failed_audio else 'generate',
                status='failed' if failed_audio else 'succeeded',
                error_code=service._controlled_failure(config_error(audio_error)) if failed_audio else None,
                duration_ms=0,publish=publish)
            if not receipt['accepted']:raise config_error('config_not_found')
            if object_key and not audio_bytes:self._delete_unreferenced(object_key)
            with service.db.session() as sess:
                current=sess.get(PreparedFirstSegmentRow,row.cache_id)
                consumed=current.consumed_conversation_id if current else None
            if consumed:self.attach_audio(consumed,row.owner_user_id)
            self.enforce_limits(row.owner_user_id)
            return {'status':'failed' if failed_audio else 'succeeded',
                **({'error_code':service._controlled_failure(config_error(audio_error))} if failed_audio else {})}
        except Exception as exc:
            if lease:service.course_preparation.note_failure(task_id,exc)
            from ..tasks.service import LeaseError
            if lease is None and isinstance(exc,LeaseError):
                if exc.code=='LANE_CAPACITY_FULL':
                    service.course_preparation.note_resource_wait(task_id,exc.resource_reason or 'capacity')
                return {'status':'queued'}
            code=service._controlled_failure(exc)
            if lease:
                service.tasks.submit_result(lease,stage='generate',status='failed',error_code=code,duration_ms=0)
            with service.db.session() as sess:
                current=sess.scalars(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.task_id==task_id)).first()
                owned=sess.get(LearningTaskRow,task_id)
                if current and (lease is None or owned and owned.attempt==lease.attempt):
                    current.status='failed';current.error_code=code
            if object_key:self._delete_unreferenced(object_key)
            return {'status':'failed','error_code':code}
        finally:
            for resource in (adapter,speech):
                if resource and hasattr(resource,'close'):resource.close()

    def _refinement_target(self,sess,task,*,locked=False):
        from ..planning.errors import error
        conv_id,_,key=task.subject_id.partition(':')
        conv=sess.get(ConversationRow,conv_id,with_for_update=locked,populate_existing=True)
        blueprint=sess.get(LessonBlueprintRow,conv_id,with_for_update=locked,populate_existing=True)
        course=sess.get(CourseRow,conv.course_id,with_for_update=locked,populate_existing=True) if conv else None
        lesson=sess.get(LessonRow,conv.lesson_id,with_for_update=locked,populate_existing=True) if conv else None
        version=sess.scalars(select(LessonContentVersionRow).where(
            LessonContentVersionRow.lesson_id==conv.lesson_id).order_by(
            LessonContentVersionRow.content_version_no.desc())).first() if conv else None
        first=sess.scalars(select(TeachingStepRow).where(TeachingStepRow.conversation_id==conv_id,
            TeachingStepRow.generation==1,TeachingStepRow.step_no==1)).first()
        if (task.kind!='lesson_plan_refine' or not conv or conv.deleted_at or conv.session_state!='active'
            or conv.owner_user_id!=task.owner_user_id or not course or course.deleted_at
            or course.owner_user_id!=conv.owner_user_id or course.current_outline_version_id!=conv.outline_version_id
            or not lesson or lesson.outline_version_id!=conv.outline_version_id or not blueprint or not first
            or blueprint.owner_user_id!=conv.owner_user_id or blueprint.lesson_id!=conv.lesson_id
            or blueprint.outline_version_id!=conv.outline_version_id
            or blueprint.content_version_id!=conv.lesson_content_version_id
            or version and version.content_version_id!=conv.lesson_content_version_id):
            raise error('TASK_CANCELLED','refinement target changed')
        context={name:value for name,value in json.loads(blueprint.context_json).items() if not name.startswith('_')}
        if context!=lesson_context(self.teaching,conv) or plan_key(self.teaching,conv,context)!=key:
            raise error('TASK_CANCELLED','refinement snapshot changed')
        snapshot=self.teaching.prompts.resolve_for_task(task.task_id,'lesson_blueprint_v1')
        if snapshot['template_sha256']!=self.teaching.prompts.load_local_template('lesson_blueprint_v1')['sha256']:
            raise error('TASK_CANCELLED','refinement template changed')
        return conv,context,key

    def refinement_eligible(self,sess,task):
        from ..planning.errors import PlanningError
        from ..prompts.service import PromptUnavailable
        try:
            conv,_,key=self._refinement_target(sess,task)
        except (PlanningError,PromptUnavailable,ValueError,TypeError):
            return False
        cached=sess.get(PreparedLessonPlanRow,key)
        return not cached or (cached.owner_user_id==conv.owner_user_id and cached.course_id==conv.course_id
            and cached.lesson_id==conv.lesson_id and cached.source!='agent')

    def queue_refinement(self,conv_id,owner):
        service=self.teaching
        model=self._model(owner)
        if getattr(service,"configs",None):
            from ..agents.model_routing import role_config
            model=role_config(service.configs,owner,"planning")
        if not model:return {'status':'unconfigured'}
        with service.db.session() as sess:
            conv=sess.get(ConversationRow,conv_id)
            blueprint=sess.get(LessonBlueprintRow,conv_id)
            first=sess.scalars(select(TeachingStepRow).where(TeachingStepRow.conversation_id==conv_id,
                TeachingStepRow.generation==1,TeachingStepRow.step_no==1)).first()
            course=sess.get(CourseRow,conv.course_id) if conv else None
            if (not conv or conv.owner_user_id!=owner or conv.deleted_at or conv.session_state!='active'
                or not blueprint or blueprint.owner_user_id!=owner or not first or not course or course.deleted_at
                or course.owner_user_id!=owner or course.current_outline_version_id!=conv.outline_version_id):
                return {'status':'unavailable'}
            context={name:value for name,value in json.loads(blueprint.context_json).items() if not name.startswith('_')}
            if context!=lesson_context(service,conv):return {'status':'unavailable'}
            key=plan_key(service,conv,context)
            cached=sess.get(PreparedLessonPlanRow,key)
            if cached:
                if (cached.owner_user_id!=owner or cached.course_id!=conv.course_id or cached.lesson_id!=conv.lesson_id):
                    return {'status':'unavailable'}
                if cached.source=='agent':return {'status':'ready'}
            from ..slice1.errors import Slice1Error
            try:service.authorization.require(sess,conv)
            except Slice1Error as exc:return {'status':'authorization_required','error_code':exc.code}
            source_task_id=blueprint.source_task_id
        from ..prompts.service import PromptUnavailable
        try:snapshot=service.prompts.resolve_for_task(source_task_id,'lesson_blueprint_v1')
        except PromptUnavailable:return {'status':'unavailable'}
        if snapshot['template_sha256']!=service.prompts.load_local_template('lesson_blueprint_v1')['sha256']:
            return {'status':'unavailable'}
        from ..agents.task_binding import AgentBudgetRow
        with service.db.session() as sess:
            conv=sess.get(ConversationRow,conv_id,populate_existing=True)
            service.authorization.require(sess,conv)
            created=service.tasks.create_task(owner_user_id=owner,kind='lesson_plan_refine',
                subject_id=conv_id+':'+key,idempotency_key='plan-refine:'+conv_id+':'+key,
                stage='generate',lane='maintenance',deadline_seconds=180,
                model_config_version=f"{model['config_id']}:{model['config_version']}",session=sess)
            service.authorization.bind_task(sess,conv,created['task_id'])
            budget=sess.get(AgentBudgetRow,created['task_id'],with_for_update=True)
            budget.request_limit=min(budget.request_limit,3)
        service.prompts.clone_for_task(created['task_id'],source_task_id,'lesson_blueprint_v1')
        return created

    def run_refinement(self,task_id):
        from dataclasses import replace
        from contextvars import copy_context
        from ..agents.task_binding import AgentBudgetRow,bind_task_deps
        from ..planning.errors import PlanningError,error
        from ..prompts.service import PromptUnavailable
        from ..tasks.service import LeaseError
        from .pedagogy import refine_plan
        service=self.teaching;adapter=None;lease=None
        try:
            lease=service.tasks.claim(task_id,lease_owner='lesson-plan-refine',lease_seconds=120,allow_reclaim_active=False)
            with service.db.session() as sess:
                task=service.tasks.validate_lease(lease,session=sess)
                conv,context,key=self._refinement_target(sess,task)
                budget=sess.get(AgentBudgetRow,task_id,with_for_update=True)
                budget.request_limit=min(budget.request_limit,3)
                sess.expunge(conv)
            service._check_authorization(task_id)
            adapter=service._model_adapter(conv.owner_user_id,task_id)
            if adapter is None:raise error('MODEL_CAPABILITY_UNAVAILABLE')
            bound=getattr(adapter,'agent_deps',None) or bind_task_deps(service.db,adapter,
                actor_id=conv.owner_user_id,task_id=task_id)
            original_check=bound.check_state
            def check_refinement():
                if original_check:original_check()
                with service.db.session() as sess:
                    current=service.tasks.validate_lease(lease,session=sess)
                    self._refinement_target(sess,current)
            adapter.agent_deps=replace(bound,check_state=check_refinement,run_request_limit=3)
            adapter.agent_deps.budget.request_limit=min(bound.budget.request_limit,3)
            adapter.agent_deps.budget.wire_limit=min(bound.budget.wire_limit,3)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future=pool.submit(copy_context().run,refine_plan,service,conv,task_id,adapter,context)
                while True:
                    try:plan,_=future.result(timeout=30);break
                    except FutureTimeout:service.tasks.heartbeat(lease,lease_seconds=120)
            check_refinement()
            def publish(sess):
                service._check_authorization(task_id,session=sess)
                current=service.tasks.validate_lease(lease,session=sess)
                live,_,live_key=self._refinement_target(sess,current,locked=True)
                cached=sess.get(PreparedLessonPlanRow,live_key,with_for_update=True)
                if cached:
                    if (cached.owner_user_id!=live.owner_user_id or cached.course_id!=live.course_id
                        or cached.lesson_id!=live.lesson_id):raise error('TASK_CANCELLED')
                    if cached.source=='agent':return
                    cached.plan_json=plan.model_dump_json();cached.source='agent'
                else:
                    sess.add(PreparedLessonPlanRow(cache_key=key,owner_user_id=live.owner_user_id,
                        course_id=live.course_id,lesson_id=live.lesson_id,plan_json=plan.model_dump_json(),source='agent'))
            receipt=service.tasks.submit_result(lease,stage='generate',status='succeeded',duration_ms=0,publish=publish)
            return {'status':'succeeded' if receipt['accepted'] else 'stale_discarded'}
        except LeaseError:
            if lease is None:return {'status':'unavailable'}
            service.tasks.recover_worker_failure(lease,stage='generate',error_code='LEASE_LOST')
            return {'status':'stale_discarded'}
        except Exception as exc:
            code=service._controlled_failure(exc)
            if lease:
                service.tasks.submit_result(lease,stage='generate',status='failed',error_code=code,duration_ms=0)
            return {'status':'failed','error_code':code}
        finally:
            if adapter is not None and hasattr(adapter,'close'):adapter.close()

    def attach_audio(self,conv_id,owner):
        self.queue_refinement(conv_id,owner)
        with self.teaching.db.session() as sess:
            conv=sess.get(ConversationRow,conv_id)
            if not conv or conv.deleted_at or conv.owner_user_id!=owner:return
            from ..characters.models import ClassroomPlaybackRow
            if sess.get(ClassroomPlaybackRow,conv_id):
                return
            row=self._row_for_conv(sess,conv)
            if not row or row.candidate_json=='{}':return
            step=sess.scalars(select(TeachingStepRow).where(TeachingStepRow.conversation_id==conv_id,TeachingStepRow.step_no==1,TeachingStepRow.generation==1)).first()
            if not step:return
            if row.audio_object_key and self.keys(conv)[1]==row.audio_key and not step.audio_id:
                from ..slice1.errors import Slice1Error
                try:self.teaching.authorization.require(sess,conv,audio=True)
                except Slice1Error:
                    row.status='consumed';return
                voice=self._audio(owner)
                if voice:
                    try:self.teaching.tts_adapter_factory.disclosures.verify_or_audit(task_id=row.task_id,
                        config_id=voice['config_id'],content_category='audio_text',actor_id=owner,session=sess,
                        scope_snapshot={'course_id':row.course_id,'lesson_id':row.lesson_id,'outline_version_id':json.loads(row.context_json)['outline_version_id']})
                    except Exception:
                        row.status='consumed';return
                meta=json.loads(row.audio_json)
                audio=AudioAssetRow(audio_id='aud_'+uuid.uuid4().hex[:12],conversation_id=conv_id,generation=1,step_no=1,
                    text_version_no=step.text_version_no,text_sha256=hashlib.sha256(step.text.encode()).hexdigest(),storage_key=row.audio_object_key,
                    status='ready',audio_format=meta['audio_format'],subtitle_json=json.dumps(meta['subtitle'],ensure_ascii=False),duration_ms=meta['duration_ms'])
                sess.add(audio);step.audio_id=audio.audio_id;row.audio_object_key='';row.audio_bytes=0
            row.status='consumed'

    def _delete_unreferenced(self,key):
        with self.teaching.db.session() as sess:
            if self._pinned(sess,key):return
            if sess.scalar(select(func.count()).select_from(AudioAssetRow).where(AudioAssetRow.storage_key==key)):return
            if sess.scalar(select(func.count()).select_from(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.audio_object_key==key)):return
        try:self.teaching.storage.delete(key)
        except Exception:pass

    def cleanup(self):
        keys=[]
        with self.teaching.db.session() as sess:
            rows=list(sess.scalars(select(PreparedFirstSegmentRow).join(CourseRow,CourseRow.course_id==PreparedFirstSegmentRow.course_id)
                .where(((PreparedFirstSegmentRow.persistent.is_(False)) & (PreparedFirstSegmentRow.expires_at<=utcnow()))
                       |CourseRow.deleted_at.is_not(None)).limit(500)))
            for row in rows:
                if row.audio_object_key and self._pinned(sess,row.audio_object_key):continue
                course=sess.get(CourseRow,row.course_id)
                if not course or course.deleted_at or fresh(row.expires_at)<=utcnow():
                    active=sess.get(ConversationRow,row.consumed_conversation_id) if row.consumed_conversation_id else None
                    if course and not course.deleted_at and active and not active.deleted_at and active.session_state in ('active','paused','awaiting_resume'):
                        continue
                    if row.audio_object_key:keys.append(row.audio_object_key)
                    sess.delete(row)
        for key in keys:self._delete_unreferenced(key)
        base=getattr(self.teaching.storage,'base_dir',None)
        if base:
            root=base/'prepared'
            if root.exists():
                checked=0
                for file in sorted(root.rglob('*')):
                    if not file.is_file():continue
                    relative=file.relative_to(base).as_posix()
                    if relative<=self._gc_cursor:continue
                    checked+=1
                    self._gc_cursor=relative
                    if utcnow().timestamp()-file.stat().st_mtime>3600:
                        self._delete_unreferenced(relative)
                    if checked>=500:break
                else:self._gc_cursor=''

    def enforce_limits(self,owner):
        garbage=[]
        with self.teaching.db.session() as sess:
            rows=list(sess.scalars(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.owner_user_id==owner,
                PreparedFirstSegmentRow.persistent.is_(False),
                PreparedFirstSegmentRow.consumed_conversation_id.is_(None)).order_by(PreparedFirstSegmentRow.created_at)))
            rows=[row for row in rows if not row.audio_object_key or not self._pinned(sess,row.audio_object_key)]
            def size(item):return item.audio_bytes+len(item.candidate_json.encode())+len(item.context_json.encode())+len(item.plan_json.encode())
            while rows and (len(rows)>self.max_entries or len({item.course_id for item in rows})>self.max_courses or sum(size(item) for item in rows)>self.max_bytes):
                old=rows.pop(0)
                if old.audio_object_key:garbage.append(old.audio_object_key)
                sess.delete(old)
        for key in garbage:self._delete_unreferenced(key)

    def close(self):pass

    def resource_wait_reason(self):
        from ..cleanup.budget import ResourceBudget, current_budget
        budget=getattr(self.teaching.tasks,'resource_budget',None) or current_budget()
        if budget is None:
            budget=getattr(self,'_resource_budget',None)
            if budget is None:
                budget=self._resource_budget=ResourceBudget(self.teaching.storage)
        return budget.wait_reason()

    def _pinned(self,sess,key):
        from sqlalchemy import inspect
        from ..cleanup.models import LocalResourceRow
        if not inspect(sess.bind).has_table(LocalResourceRow.__tablename__):return False
        return bool(sess.scalar(select(LocalResourceRow.resource_id).where(
            LocalResourceRow.object_key==key,LocalResourceRow.pinned.is_(True)).limit(1)))

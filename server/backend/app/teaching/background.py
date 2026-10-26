"""Bounded workers continuously buffer an observed lesson up to dialogue boundaries."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event,Lock,Thread
from time import monotonic
import json
from sqlalchemy import select,update
from ..tasks.service import LearningTaskRow,LANE_CAPACITY
from ..planning.models import ConversationRow,LessonRow,utcnow
from ..prompts.service import PromptBindingRow
from .models import PlaybackCheckpointRow,TeachingStepRow
from .learning_models import LessonBlueprintRow, PreparedFirstSegmentRow


class TeachingBackground:
    def __init__(self,service):
        self.service=service;self.stop=Event();self.lock=Lock();self.pending={};self.thread=None
        self._last_reconcile=0
        self.preparation_flow=getattr(service,"course_preparation",None)
        self.pools={"first":ThreadPoolExecutor(max_workers=1,thread_name_prefix="first-prewarm"),
            "scope":ThreadPoolExecutor(max_workers=1,thread_name_prefix="scope-prepare"),
            "text":ThreadPoolExecutor(max_workers=2,thread_name_prefix="teaching-next"),
            "audio":ThreadPoolExecutor(max_workers=2,thread_name_prefix="teaching-audio"),
            "refine":ThreadPoolExecutor(max_workers=1,thread_name_prefix="lesson-refine")}

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.service.server_managed=True
        self.thread=Thread(target=self._loop,name="teaching-dispatch",daemon=True);self.thread.start()

    def _loop(self):
        cleaned=0
        while not self.stop.is_set():
            try:
                if monotonic()-cleaned>600:self.service.preparation.cleanup();cleaned=monotonic()
                self.tick()
            except Exception as exc:self._report_failure(self.service._controlled_failure(exc))
            self.stop.wait(.8)

    def eligible(self,sess,task,*,authorization_checked=False):
        from ..slice1.errors import Slice1Error
        if task.cancel_requested:return False
        if self.preparation_flow and not self.preparation_flow.eligible(sess,task):return False
        if task.kind=="unit_scope_generate":
            self.service.assessments._verify_boundary(sess,task.task_id)
            return True
        if not authorization_checked:
            try:self.service.authorization.check_task(sess,task,audio=task.kind=='tts')
            except Slice1Error:return False
        if task.kind=="lesson_plan_refine":
            return self.service.preparation.refinement_eligible(sess,task)
        if task.kind=="lesson_first_prepare":
            if not self.service.preparation.enabled:return False
            cache=sess.scalars(select(PreparedFirstSegmentRow).where(PreparedFirstSegmentRow.task_id==task.task_id)).first()
            if not cache:return False
            if not self.service.preparation._model(cache.owner_user_id):return False
            if not cache.consumed_conversation_id:return True
            conv=sess.get(ConversationRow,cache.consumed_conversation_id)
            if (cache.persistent and conv and not conv.deleted_at
                    and conv.session_state=="active"):return True
        else:
            if ":followup:" in task.subject_id:return False
            conv=sess.get(ConversationRow,task.subject_id.split(":")[0])
        if not conv or conv.deleted_at or conv.session_state!="active":return False
        if task.kind == "lesson_step_generate" and getattr(self.service, "assessments", None):
            blueprint = sess.get(LessonBlueprintRow, conv.conversation_id)
            blueprint_context = json.loads(blueprint.context_json) if blueprint else {}
            if blueprint_context.get("_scope_pending"):
                from .unit_scope import UnitScopeRow
                lesson = sess.get(LessonRow, conv.lesson_id)
                unit_id = lesson.unit_id if lesson else ""
                ready = sess.scalar(select(UnitScopeRow.scope_id).join(
                    LearningTaskRow, UnitScopeRow.task_id == LearningTaskRow.task_id).where(
                        UnitScopeRow.owner_user_id == conv.owner_user_id,
                        UnitScopeRow.unit_id == unit_id,
                        UnitScopeRow.input_hash == blueprint_context.get("_scope_pending_input_hash"),
                        UnitScopeRow.outline_version_id == conv.outline_version_id,
                        LearningTaskRow.status == "succeeded").limit(1))
                if not ready:return False
        if task.kind!='lesson_first_prepare' and monotonic()-self.service.observed_sessions.get(conv.conversation_id,0)>90:return False
        checkpoint=sess.scalars(select(PlaybackCheckpointRow).where(PlaybackCheckpointRow.conversation_id==conv.conversation_id)
            .order_by(PlaybackCheckpointRow.client_seq.desc())).first()
        if checkpoint and self.service._presentation(json.loads(checkpoint.board_state_json)).get('paused'):
            cache=self.service.preparation._row_for_conv(sess,conv)
            from .preparation_models import PreparationItemRow
            durable=sess.scalar(select(PreparationItemRow.item_id).where(
                PreparationItemRow.task_id==task.task_id,
                PreparationItemRow.state.in_(("waiting","running"))))
            if not (task.kind=="lesson_step_generate" and task.subject_id.split(":")[1]=="1"
                    and ((cache and cache.persistent and cache.candidate_json!="{}") or durable)):return False
        if task.kind=="lesson_first_prepare":return True
        number=int(task.subject_id.split(":")[1]);current=max(1,checkpoint.step_no if checkpoint else 1)
        if number<current or not self.service._buffer_step_allowed(sess,conv,number):return False
        if task.kind=="lesson_step_generate":
            if number>1 and not self.service._step_artifact(sess,conv,number-1)[2]:return False
            if not sess.scalar(select(PromptBindingRow.binding_id).where(PromptBindingRow.task_id==task.task_id,
                PromptBindingRow.prompt_name=='teaching_step_v1')):return False
            return not self.service.preparation.waiting({"kind":task.kind,"subject_id":task.subject_id})
        return True

    def tick(self):
        for ident,last in list(self.service.observed_sessions.items()):
            if monotonic()-last>180 and self.service.observed_sessions.get(ident)==last:
                self.service.observed_sessions.pop(ident,None)
        with self.lock:
            self.pending={task_id:value for task_id,value in self.pending.items() if not value[1].done()}
            counts={group:sum(value[0]==group for value in self.pending.values()) for group in self.pools}
        self.service.tasks.sweep_expired_tasks(kinds=['lesson_first_prepare','lesson_step_generate','tts','lesson_plan_refine','unit_scope_generate'])
        from .recovery import recover_observed
        try:
            recover_observed(self)
        except Exception as exc:
            self._report_failure(self.service._controlled_failure(exc))
        with self.service.db.session() as sess:
            now=utcnow()
            tasks=list(sess.scalars(select(LearningTaskRow).where(LearningTaskRow.kind.in_(['lesson_first_prepare','lesson_step_generate','tts','lesson_plan_refine','unit_scope_generate']),
                LearningTaskRow.cancel_requested.is_(False),
                LearningTaskRow.status.in_(['queued','leased','running']),LearningTaskRow.deadline_at>now,
                (LearningTaskRow.status=='queued')|(LearningTaskRow.lease_until<=now)).order_by(LearningTaskRow.created_at).limit(100)))
            positions={}
            if self.service.observed_sessions:
                for point in sess.scalars(select(PlaybackCheckpointRow).where(
                        PlaybackCheckpointRow.conversation_id.in_(list(self.service.observed_sessions)))
                        .order_by(PlaybackCheckpointRow.client_seq.desc())):
                    positions.setdefault(point.conversation_id,max(1,point.step_no))
            def priority(task):
                from .preparation_models import PreparationItemRow
                item=sess.scalar(select(PreparationItemRow).where(PreparationItemRow.task_id==task.task_id)) if self.preparation_flow and self.preparation_flow.enabled else None
                parts=task.subject_id.split(":")
                distance=(max(0,int(parts[1])-positions.get(parts[0],1))
                    if task.kind in ("lesson_step_generate","tts") and parts[1].isdigit() else 0)
                return (task.lane!='interactive',item.priority if item else -3,task.kind=='lesson_plan_refine',
                    task.kind=='lesson_first_prepare',distance,task.kind!='tts',task.created_at)
            tasks.sort(key=priority)
            live=list(sess.scalars(select(LearningTaskRow).where(LearningTaskRow.status.in_(['leased','running']),
                LearningTaskRow.lease_until>now)))
            lane_counts={lane:sum(task.lane==lane for task in live) for lane in LANE_CAPACITY.lane_limits}
            owner_counts={}
            for item in live:owner_counts[(item.owner_user_id,item.lane)]=owner_counts.get((item.owner_user_id,item.lane),0)+1
            selected=[];denied=[];conversations={value[2] for value in self.pending.values() if value[0]=='text'}
            from ..slice1.errors import Slice1Error
            for task in tasks:
                group=('first' if task.kind=='lesson_first_prepare' else 'audio' if task.kind=='tts'
                    else 'refine' if task.kind=='lesson_plan_refine' else 'scope' if task.kind=='unit_scope_generate' else 'text')
                limit=1 if group in ('first','refine','scope') else 2
                if counts[group]>=limit or task.task_id in self.pending:continue
                try:
                    with self.service.db.session() as check:
                        live_task=check.get(LearningTaskRow,task.task_id,with_for_update=True)
                        if not live_task or live_task.status not in ('queued','leased','running'):
                            continue
                        if live_task.kind!="unit_scope_generate":
                            self.service.authorization.check_task(check,live_task,audio=live_task.kind=='tts')
                        if not self.eligible(check,live_task,authorization_checked=True):
                            continue
                except Slice1Error as exc:
                    if task.status=='queued':denied.append((task.task_id,task.generation,task.revision,self.service._controlled_failure(exc)))
                    continue
                owner_lane=(task.owner_user_id,task.lane)
                if sum(lane_counts.values())>=LANE_CAPACITY.global_limit or lane_counts[task.lane]>=LANE_CAPACITY.lane_limits[task.lane]:continue
                if task.lane!='interactive' and sum(value for lane,value in lane_counts.items() if lane!='interactive')>=LANE_CAPACITY.non_interactive_capacity:continue
                if LANE_CAPACITY.per_user_per_lane>0 and owner_counts.get(owner_lane,0)>=LANE_CAPACITY.per_user_per_lane:continue
                if group=='text':
                    conv=task.subject_id.split(':')[0]
                    if conv in conversations:continue
                    conversations.add(conv)
                if group=='first' and task.lane!='interactive' and (counts['text'] or counts['audio']):continue
                counts[group]+=1;lane_counts[task.lane]+=1;owner_counts[owner_lane]=owner_counts.get(owner_lane,0)+1
                selected.append((task.task_id,group,task.subject_id.split(':')[0]))
            active=list(sess.scalars(select(ConversationRow).where(ConversationRow.kind=='teaching',ConversationRow.session_state=='active',
                ConversationRow.deleted_at.is_(None),ConversationRow.conversation_id.in_(
                    [key for key,value in list(self.service.observed_sessions.items()) if monotonic()-value<90]))))
        for task_id,generation,revision,code in denied:
            self.service.tasks.fail_queued(task_id,expected_generation=generation,expected_revision=revision,error_code=code)
        for task_id,group,subject in selected:
            with self.lock:self.pending[task_id]=(group,self.pools[group].submit(self._execute,task_id,group),subject)
        for conv in active:
            try:
                with self.service.db.session() as sess:
                    checkpoint=sess.scalars(select(PlaybackCheckpointRow).where(PlaybackCheckpointRow.conversation_id==conv.conversation_id)
                        .order_by(PlaybackCheckpointRow.client_seq.desc())).first()
                    if checkpoint and self.service._presentation(json.loads(checkpoint.board_state_json)).get('paused'):continue
                    current=max(1,checkpoint.step_no if checkpoint else 1)
                    self.service._ensure_prefetch(sess,conv.conversation_id,conv.owner_user_id,current+1,conv.generation)
                if getattr(self.service.tts_adapter_factory,'config_for_owner',None):
                    if not self.service.tts_adapter_factory.config_for_owner(conv.owner_user_id):continue
                    with self.service.db.session() as sess:
                        steps=list(sess.scalars(select(TeachingStepRow).where(TeachingStepRow.conversation_id==conv.conversation_id,
                            TeachingStepRow.generation==1,TeachingStepRow.step_no>=current,
                            TeachingStepRow.status=="ready",TeachingStepRow.audio_id.is_(None))
                            .order_by(TeachingStepRow.step_no)))
                        steps=[step for step in steps if self.service._buffer_step_allowed(sess,conv,step.step_no)]
                    for step in steps:self.service.ensure_audio(conv.conversation_id,actor_id=conv.owner_user_id,step_no=step.step_no)
            except Exception as exc:self._report_failure(self.service._controlled_failure(exc))
        if monotonic()-self._last_reconcile>=15:
            self._last_reconcile=monotonic()
            for conv in active[:4]:
                try:
                    if getattr(self.service, "assessments", None):
                        with self.service.db.session() as sess:
                            blueprint = sess.get(LessonBlueprintRow, conv.conversation_id)
                            pending_scope = bool(blueprint and json.loads(blueprint.context_json).get("_scope_pending"))
                            lesson = sess.get(LessonRow, conv.lesson_id) if pending_scope else None
                        if lesson and not (self.preparation_flow and self.preparation_flow.enabled):
                            self.service.assessments.scopes.resume(conv.course_id, lesson.unit_id, conv.owner_user_id)
                    self.service.preparation.prepare_next(conv.conversation_id,conv.owner_user_id)
                except Exception as exc:self._report_failure(self.service._controlled_failure(exc))
            try:self.service.preparation.reconcile(limit=4)
            except Exception as exc:self._report_failure(self.service._controlled_failure(exc))

    def _execute(self,task_id,group):
        try:
            if group=='scope':return self.service.assessments.scopes._run(task_id)
            if group=='first':return self.service.preparation.run(task_id)
            if group=='audio':return self.service.run_tts(task_id)
            if group=='refine':return self.service.preparation.run_refinement(task_id)
            return self.service.run_step_generation(task_id)
        except Exception as exc:
            code=self.service._controlled_failure(exc)
            self._report_failure(code)
            from ..tasks.service import Lease
            with self.service.db.session() as sess:
                task=sess.get(LearningTaskRow,task_id)
                lease=Lease(task.task_id,task.attempt_id,task.lease_token,task.lease_owner,
                    task.generation,task.attempt,task.lease_until,task.deadline_at,task.owner_user_id) if task and task.lease_token else None
            if lease:
                self.service.tasks.recover_worker_failure(lease,stage='tts' if group=='audio' else 'generate',error_code=code)
            return {'status':'failed','error_code':code}

    def _report_failure(self,code):
        from ..logging_audit.metrics import record_task_diagnostic
        record_task_diagnostic(reason='worker_recovery_failed',error_code=code)

    def close(self):
        self.stop.set();self.service.server_managed=False
        if self.thread:self.thread.join(timeout=2)
        for pool in self.pools.values():pool.shutdown(wait=False,cancel_futures=True)

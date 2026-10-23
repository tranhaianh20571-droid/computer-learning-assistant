"""Checkpointed Nuwa workflow. Each stage runs a fresh Pydantic AI agent."""
import hashlib
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextvars import copy_context
from datetime import datetime, timezone
from time import monotonic
from types import SimpleNamespace
from sqlalchemy import select

from ..agents.model_routing import pin_role, role_config
from ..agents.runner import run_structured_agent, run_streaming_structured_agent
from ..agents.task_binding import AgentBudgetRow
from ..planning.errors import error
from ..prompts.service import PromptBindingRow
from ..tasks.service import LearningTaskRow
from .distillation_models import (CharacterBuildRow, CharacterBuildStageRow, ResearchReport,
    PersonaExtraction, BuiltPersona, PersonaTests, PersonaReply, QualityReport, Refinement,
    CharacterCandidates, SourceReview, SourceAssessment, ResearchPlan, ResearchPolicy, BuildRequest,
    PersonaAcceptance, MentorAcceptance, PersonaModels, PersonaTraits, FastResearch)
from .acceptance import acceptance_quality
from .evidence import (canonical_source, page_status, body_hash, verified_assessment,
    evidence_summary, evidence_checks, unmeasured_style_numbers, DIMENSIONS)
from . import distillation_prompts as prompts
from .models import CharacterCardRow
from .persona import PersonaPackage
from .schemas import Profile
from .service import reject

POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="persona-build")


def encode(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def stage_payload(request,payload):
    if request.get("pipeline_version",1)<6:return payload
    subject=payload.get("character_subject",request.get("subject",{}))
    return {**payload,"character_subject":subject,
        "research_dimensions":{key:value[0] for key,value in prompts.dimensions_for(subject).items()}}


def build_domain(sess, task, *, allow_terminal=False):
    if task.kind == "character_build":
        row=sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id==task.task_id))
    else:
        stage=sess.get(CharacterBuildStageRow,task.task_id)
        row=sess.get(CharacterBuildRow,stage.build_id) if stage else None
    root=sess.get(LearningTaskRow,row.task_id) if row else None
    card=sess.get(CharacterCardRow,row.card_id) if row else None
    if (not row or row.owner_user_id!=task.owner_user_id or not root or root.cancel_requested
        or not allow_terminal and root.status in ("failed","cancelled","expired")
        or not allow_terminal and root.deadline_at and utc_deadline(root.deadline_at)<=datetime.now(timezone.utc)
        or not card or card.deleted_at or card.current_revision!=row.card_revision):
        raise error("TASK_CANCELLED","character build no longer authorized")
    from ..slice1.models import ServiceConfigRow
    config=sess.get(ServiceConfigRow,row.config_id,populate_existing=True)
    if (not config or not config.is_active or config.owner_scope!="user"
        or config.owner_user_id!=row.owner_user_id or config.kind!="content_model"
        or config.config_version!=row.config_version):
        raise error("DISCLOSURE_REVOKED","character build configuration changed")
    from ..agents.model_routing import TaskModelRoleRow
    for role in ("evidence_search","research_search"):
        pinned=sess.get(TaskModelRoleRow,(root.task_id,role))
        if pinned:
            search=sess.get(ServiceConfigRow,pinned.config_id,populate_existing=True)
            if (not search or not search.is_active or search.owner_user_id!=row.owner_user_id
                    or search.owner_scope!="user" or search.config_version!=pinned.config_version):
                raise error("DISCLOSURE_REVOKED","research search configuration changed")
    scope={"build_id":row.build_id,"card_id":row.card_id,"revision":row.card_revision}
    request=json.loads(row.request_json)
    if request.get("reuse_build_id"):
        scope.update(reuse_build_id=request["reuse_build_id"],reuse_sources_sha256=request["reuse_sources_sha256"])
    return scope,(row.build_id,row.request_hash,row.config_id,row.config_version),"query"


def utc_deadline(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class DistillationService:
    pipeline_version=7

    def __init__(self,cards,tasks,prompts_service,disclosures,model_factory,search_factory=None,media_service=None):
        self.cards,self.db,self.tasks,self.prompts,self.disclosures=cards,cards.db,tasks,prompts_service,disclosures
        self.model_factory,self.search_factory=model_factory,search_factory
        self.media_service=media_service
        self.lock,self.running=threading.Lock(),set()
        self.stop_event,self.thread=threading.Event(),None

    def start(self):
        if self.thread:return
        self.thread=threading.Thread(target=self._recover,name="persona-recovery",daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=2)

    def _recover(self):
        while not self.stop_event.is_set():
            try:
                with self.lock:
                    capacity=max(0,2-len(self.running))
                if capacity:
                    with self.db.session() as sess:
                        now=datetime.now(timezone.utc)
                        rows=list(sess.execute(select(CharacterBuildRow.build_id,CharacterBuildRow.owner_user_id)
                            .join(LearningTaskRow,LearningTaskRow.task_id==CharacterBuildRow.task_id)
                            .where(LearningTaskRow.cancel_requested.is_(False),
                                LearningTaskRow.deadline_at>now,
                                (LearningTaskRow.status=="queued")|(
                                    LearningTaskRow.status.in_(("running","leased"))&
                                    (LearningTaskRow.lease_until<=now)))
                            .order_by(CharacterBuildRow.created_at).limit(30)))
                    for ident,owner in rows:
                        with self.lock:
                            if len(self.running)>=2:break
                        self.get(owner,ident)
            except Exception:
                pass  # Recovery never emits private inputs or raw provider errors.
            self.stop_event.wait(10)

    def create(self,owner,request):
        if not request.confirmed:
            reject("disclosure_required",409)
        if request.mode=="text" and not request.source_text.strip():
            reject("invalid_field",422)
        view=role_config(self.cards.configs,owner,"research")
        if not view:
            reject("config_not_found",409,message="请先在设置页配置蒸馏模型。")
        data={**request.model_dump(exclude={"request_key","confirmed"}),"pipeline_version":self.pipeline_version}
        if request.dialogue and (request.mode!="ai" or not request.message.strip()):
            reject("invalid_field",422)
        if request.complete_card:
            if not request.dialogue or self.media_service is None:
                reject("invalid_field",422)
            from .creation_delivery import freeze_configs
            data["delivery_configs"]=freeze_configs(self,owner)
        if self.pipeline_version>=7 and not request.dialogue:
            data["context_markers"]=True
            data["research_policy"]={**data["research_policy"],
                "max_searches":1,"max_reads":min(6,request.research_policy.max_reads),"max_candidates":10}
        search_view=None
        if request.dialogue:
            search_view=self.cards.configs.active_search_for_owner(owner)
            data["research_policy"]=ResearchPolicy(max_searches=5,max_reads=20,max_candidates=50).model_dump()
            data["dialogue_history"]=[]
            data["question_count"]=0
            data["dialogue_round"]=0
        elif request.mode=="ai" and request.subject.kind!="original":
            search_view=(role_config(self.cards.configs,owner,"research_search")
                if request.search_provider=="grok" else self.cards.configs.active_search_for_owner(owner))
            if not search_view:
                reject("config_not_found",409,message="请先配置人物研究搜索服务。")
        # Serialize duplicate creation for a user's request key across processes.
        from sqlalchemy import text
        with self.db.session() as sess:
            if sess.bind.dialect.name=="postgresql":
                sess.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key":int(hashlib.sha256((owner+request.request_key).encode()).hexdigest()[:15],16)})
                if request.dialogue and request.parent_build_id:
                    sess.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                        {"key":int(hashlib.sha256((owner+"continue:"+request.parent_build_id).encode()).hexdigest()[:15],16)})
            old=sess.scalar(select(CharacterBuildRow).where(
                CharacterBuildRow.owner_user_id==owner,CharacterBuildRow.request_key==request.request_key))
            if old:
                previous=json.loads(old.request_json)
                previous=BuildRequest.model_validate({**{key:value for key,value in previous.items()
                    if key in BuildRequest.model_fields},"request_key":request.request_key}).model_dump(
                        exclude={"request_key","confirmed"})
                expected=request.model_dump(exclude={"request_key","confirmed"})
                if request.dialogue:
                    expected["research_policy"]=data["research_policy"]
                    if request.parent_build_id and not request.source_text:
                        expected["source_text"]=previous["source_text"]
                elif json.loads(old.request_json).get("pipeline_version",1)>=7:
                    expected["research_policy"]={**expected["research_policy"],
                        "max_searches":1,"max_reads":min(6,request.research_policy.max_reads),"max_candidates":10}
                if previous!=expected:reject("state_invalid",409)
                ident=old.build_id
            else:
                reused=[]
                if request.dialogue and request.parent_build_id:
                    parent=self._owned(sess,owner,request.parent_build_id)
                    parent_task=sess.get(LearningTaskRow,parent.task_id)
                    parent_request=json.loads(parent.request_json)
                    prior=json.loads(parent.result_json).get("dialogue")
                    if (parent_task.status!="succeeded" or not prior or parent.accepted_revision
                            or parent_request.get("dialogue",False) is not True):
                        reject("state_invalid",409)
                    data["dialogue_history"]=[*parent_request.get("dialogue_history",[]),
                        {"role":"user","content":parent_request["message"]},
                        {"role":"assistant","content":prior["reply"]+"\n"+prior.get("question","")}]
                    if len(encode(data["dialogue_history"]).encode())>500000:
                        reject("invalid_field",413,message="对话材料过长，请保存人物后新建会话。")
                    data["source_text"]=request.source_text or parent_request.get("source_text","")
                    if (prior.get("awaiting_confirmation") and not prior.get("creation_confirmed")
                            and data["source_text"]==parent_request.get("source_text","")
                            and request.complete_card==parent_request.get("complete_card",False)):
                        data["pending_character"]=prior
                    data["question_count"]=prior["question_count"] if not (
                        request.source_text and not parent_request.get("source_text")) else 0
                    data["dialogue_round"]=parent_request.get("dialogue_round",0)+1
                    reused=json.loads(parent.sources_json)
                    data["searched_queries"]=parent_request.get("searched_queries",[])+[
                        json.loads(stage.state_json or "{}").get("query","") for stage in sess.scalars(
                            select(CharacterBuildStageRow).where(CharacterBuildStageRow.build_id==parent.build_id))
                        if stage.stage_key.startswith("dialogue_sources_")
                            and json.loads(stage.state_json or "{}").get("search_started")]
                    children=list(sess.scalars(select(CharacterBuildRow).where(
                        CharacterBuildRow.owner_user_id==owner,CharacterBuildRow.card_id==parent.card_id)))
                    if any(json.loads(child.request_json).get("parent_build_id")==parent.build_id for child in children):
                        reject("state_invalid",409,message="本轮已继续，请恢复最新对话。")
                elif request.parent_build_id:
                    reject("invalid_field",422)
                if request.reuse_build_id:
                    source=self._owned(sess,owner,request.reuse_build_id)
                    original=json.loads(source.request_json)
                    if (request.mode!="ai" or original["mode"]!="ai"
                            or original["topic"].strip().casefold()!=request.topic.strip().casefold()
                            or BuildRequest.model_validate({**{key:value for key,value in original.items()
                                if key in BuildRequest.model_fields},"request_key":"reuse"}).subject!=request.subject):
                        reject("invalid_field",422)
                    reused=[{**{key:value for key,value in item.items() if key not in (
                        "evidence_start","evidence_end","reason")},"kind":"unknown","evidence":"","dimensions":[],"usable":False,
                        "reused_from":source.build_id} for item in json.loads(source.sources_json)
                        if item.get("read_status") in ("full_text","partial_text")
                            and page_status(item.get("text",""))=="readable"][:20]
                    unique=[]
                    seen_urls=set()
                    seen_bodies=set()
                    for source in reused:
                        url=canonical_source(source.get("url",""))
                        fingerprint=body_hash(source["text"])
                        if not url or url in seen_urls or fingerprint in seen_bodies:
                            continue
                        seen_urls.add(url)
                        seen_bodies.add(fingerprint)
                        unique.append({**source,"canonical_url":url,"body_sha256":fingerprint})
                    reused=unique
                    data["reuse_sources_sha256"]=digest(reused)
                from .models import CharacterRevisionRow
                if request.dialogue and request.parent_build_id:
                    card=sess.get(CharacterCardRow,parent.card_id)
                    if card.current_revision!=parent.card_revision:reject("state_invalid",409)
                else:
                    card=CharacterCardRow(card_id="card_"+uuid.uuid4().hex,owner_user_id=owner,current_revision=1)
                    sess.add(card);sess.flush()
                    sess.add(CharacterRevisionRow(card_id=card.card_id,revision=1,
                        profile_json=Profile(name=request.topic[:80]).model_dump_json()))
                task=self.tasks.create_task(owner_user_id=owner,kind="character_build",
                    subject_id=card.card_id,idempotency_key="persona:"+request.request_key,
                    stage="generate",deadline_seconds=2400 if request.complete_card else
                        600 if self.pipeline_version>=7 else 7200,session=sess)
                ident="build_"+uuid.uuid4().hex
                row=CharacterBuildRow(build_id=ident,owner_user_id=owner,task_id=task["task_id"],
                    card_id=card.card_id,card_revision=card.current_revision,request_key=request.request_key,
                    request_json=encode(data),request_hash=digest(data),config_id=view["config_id"],
                    config_version=view["config_version"],sources_json=encode(reused))
                sess.add(row);sess.flush()
                scope,_,category=build_domain(sess,sess.get(LearningTaskRow,task["task_id"]))
                grant=self.disclosures.create(actor_id=owner,task_id=task["task_id"],
                    config_id=view["config_id"],config_version=view["config_version"],
                    content_category=category,scope_snapshot=scope,session=sess)
                pin_role(sess,task["task_id"],"research",view,grant["grant_id"])
                if search_view:
                    search_grant=self.disclosures.create(actor_id=owner,task_id=task["task_id"],
                        config_id=search_view["config_id"],config_version=search_view["config_version"],
                        content_category=category,scope_snapshot=scope,session=sess)
                    pin_role(sess,task["task_id"],
                        "research_search" if request.search_provider=="grok" else "evidence_search",
                        search_view,search_grant["grant_id"])
                budget=sess.get(AgentBudgetRow,task["task_id"])
                budget.request_limit,budget.tool_limit=(24 if self.pipeline_version>=7 else
                    256 if self.pipeline_version>=6 else 128),256
        self.kick(ident)
        return self.get(owner,ident)

    def get(self,owner,ident):
        with self.db.session() as sess:
            row=self._owned(sess,owner,ident)
            task=sess.get(LearningTaskRow,row.task_id)
            stages=list(sess.scalars(select(CharacterBuildStageRow).where(
                CharacterBuildStageRow.build_id==ident)))
            items=[]
            for stage in stages:
                current=sess.get(LearningTaskRow,stage.task_id)
                items.append({"stage":stage.stage_key,"task_id":stage.task_id,"status":current.status,
                    "error_code":self.tasks.get_task(current.task_id)["error_code"],"output":json.loads(stage.result_json),
                    "metrics":{key:value for key,value in json.loads(stage.state_json or "{}").items()
                        if key in ("duration_ms","first_output_ms","model_requests","wire_requests",
                            "read_attempts","candidate_count","search_started","search_metrics","interrupted")}})
            sources=json.loads(row.sources_json)
            summary=evidence_summary(sources)
            summary.update(search_calls=sum(bool(json.loads(stage.state_json or "{}").get("search_started")) for stage in stages),
                reused_documents=sum(bool(item.get("reused_from")) for item in sources),
                readable_documents=sum(item.get("read_status") in ("full_text","partial_text") for item in sources),
                read_attempts=sum(json.loads(stage.state_json or "{}").get("read_attempts",0) for stage in stages),
                provider_actions=sum(json.loads(stage.state_json or "{}").get("search_metrics",{}).get(
                    "completed_actions",0) for stage in stages))
            result={"build_id":ident,"card_id":row.card_id,"revision":row.card_revision,
                "accepted_revision":row.accepted_revision,"task_id":row.task_id,"status":task.status,
                "error_code":self.tasks.get_task(task.task_id)["error_code"],"stages":items,"result":json.loads(row.result_json),
                "source_count":len(sources),"evidence":summary,"request":json.loads(row.request_json)}
            if result["request"].get("pipeline_version",1)>=7:
                end=task.updated_at if task.status in ("succeeded","failed","cancelled","expired") else datetime.now(timezone.utc)
                result["timing"]={"target_seconds":2400 if result["request"].get("complete_card") else 600,
                    "elapsed_seconds":max(0,int((utc_deadline(end)-utc_deadline(row.created_at)).total_seconds()))}
            restart=task.status in ("queued","expired") or (task.status in ("running","leased")
                and task.lease_until and self.tasks._as_utc(task.lease_until)<=datetime.now(timezone.utc))
        if restart:self.kick(ident)
        return result

    def _owned(self,sess,owner,ident):
        row=sess.get(CharacterBuildRow,ident)
        card=sess.get(CharacterCardRow,row.card_id) if row else None
        if not row or row.owner_user_id!=owner or not card or card.deleted_at:reject()
        return row

    def action(self,owner,ident,action,selected=""):
        with self.db.session() as sess:
            row=self._owned(sess,owner,ident)
            task=sess.get(LearningTaskRow,row.task_id)
            if action=="select":
                choices=json.loads(row.result_json).get("candidates",[])
                if selected not in choices or task.status!="succeeded":reject("state_invalid",409)
                return self._selected_request(row,selected)
            if action=="cancel":
                ids=[row.task_id,*sess.scalars(select(CharacterBuildStageRow.task_id).where(
                    CharacterBuildStageRow.build_id==ident))]
                from .models import CharacterMediaJobRow
                ids.extend(job.task_id for job in sess.scalars(select(CharacterMediaJobRow).where(
                    CharacterMediaJobRow.card_id==row.card_id))
                    if json.loads(job.request_json).get("creation",{}).get("build_id")==ident)
            elif action=="retry":
                build_domain(sess,task,allow_terminal=True)
                self.tasks.requeue_for_retry(row.task_id,actor_id=owner,
                    deadline_seconds=2400 if json.loads(row.request_json).get("complete_card") else
                        600 if json.loads(row.request_json).get("pipeline_version",1)>=7 else 7200)
                # A previous root worker may have died after creating child
                # leases. Fence those orphaned stage attempts before retrying;
                # their late writes must be rejected and their checkpoints
                # remain available for the new attempt.
                now=datetime.now(timezone.utc)
                for stage in sess.scalars(select(CharacterBuildStageRow).where(
                        CharacterBuildStageRow.build_id==ident)):
                    child=sess.get(LearningTaskRow,stage.task_id,with_for_update=True)
                    if child.status in ("leased","running"):
                        self.tasks._terminal(sess,child,status="failed",
                            error_code="TASK_LEASE_EXPIRED",now=now)
                ids=[]
            else:reject("invalid_field",422)
        for task_id in ids:self.tasks.request_cancel(task_id,actor_id=owner)
        if action=="retry":self.kick(ident)
        return self.get(owner,ident)

    @staticmethod
    def _selected_request(row,selected):
        data=json.loads(row.request_json)
        data.pop("pipeline_version",None)
        data.pop("reuse_sources_sha256",None)
        data.pop("context_markers",None)
        return {**data,"selected_character":selected,
                "request_key":uuid.uuid4().hex,"confirmed":True}

    def accept(self,owner,ident):
        with self.db.session() as sess:
            row=self._owned(sess,owner,ident)
            row=sess.get(CharacterBuildRow,ident,with_for_update=True,populate_existing=True)
            task=sess.get(LearningTaskRow,row.task_id)
            result=json.loads(row.result_json)
            if row.accepted_revision:return self.cards.get(owner,row.card_id)
            if task.status!="succeeded" or "persona" not in result:reject("state_invalid",409)
            self._accept_result(sess,row,result)
        return self.cards.get(owner,row.card_id)

    def _accept_result(self,sess,row,result):
        card=sess.get(CharacterCardRow,row.card_id,with_for_update=True)
        if card.current_revision!=row.card_revision or card.deleted_at:
            reject("state_invalid",409)
        from .models import CharacterRevisionRow
        from .persona import CharacterPersonaRow
        profile=Profile.model_validate(result["profile"])
        package=PersonaPackage.model_validate(result["persona"])
        card.current_revision+=1
        sess.add(CharacterRevisionRow(card_id=card.card_id,revision=card.current_revision,
            profile_json=self.cards._profile(sess,profile,card,row.owner_user_id)))
        sess.flush()
        sess.add(CharacterPersonaRow(card_id=card.card_id,revision=card.current_revision,
                                     payload_json=package.model_dump_json()))
        row.accepted_revision=card.current_revision
        self.cards.audit.append("config.changed",actor_id=row.owner_user_id,object_type="character_build",
            object_id=row.build_id,result="accepted",session=sess)

    def kick(self,ident):
        with self.lock:
            if ident in self.running:return
            self.running.add(ident)
        POOL.submit(copy_context().run,self.run,ident)

    def _stage(self,root,stage_key,payload,output_type,prompt,*,work=None):
        with self.db.session() as sess:
            self.tasks.validate_lease(root,session=sess)
            build=sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id==root.task_id))
            build_domain(sess,sess.get(LearningTaskRow,root.task_id))
            request=json.loads(build.request_json)
            if request.get("pipeline_version",1)>=6 and not work:
                subject=payload.get("character_subject",request.get("subject",{}))
                payload=stage_payload(request,payload)
                if not (request.get("dialogue") and stage_key=="dialogue_plan"):
                    prompt+=prompts.subject_instruction(subject)
            payload_hash=digest(payload)
            stage=sess.scalar(select(CharacterBuildStageRow).where(
                CharacterBuildStageRow.build_id==build.build_id,CharacterBuildStageRow.stage_key==stage_key))
            if request.get("pipeline_version",1)>=6 and stage_key in ("coverage_gap_plan","gap_plan"):
                fallback=sess.scalar(select(CharacterBuildStageRow).where(
                    CharacterBuildStageRow.build_id==build.build_id,
                    CharacterBuildStageRow.stage_key==stage_key+"_fallback"))
                if fallback and fallback.result_json!="{}":return None
            if stage:
                if stage.input_hash!=payload_hash:raise error("STALE_RESULT_DISCARDED")
                if stage.result_json!="{}":
                    return output_type.model_validate_json(stage.result_json) if output_type and output_type is not str else json.loads(stage.result_json)
                task_id=stage.task_id
            else:
                task=self.tasks.create_task(owner_user_id=root.owner_user_id,kind="character_build_stage",
                    subject_id=build.build_id+":"+stage_key,idempotency_key=build.build_id+":"+stage_key,
                    stage="search" if work else "generate",lane="teaching_prefetch",
                    deadline_seconds=max(1,int((utc_deadline(root.deadline_at)-datetime.now(timezone.utc)).total_seconds()))
                        if request.get("pipeline_version",1)>=7 else 3600,session=sess)
                task_id=task["task_id"]
                stage=CharacterBuildStageRow(task_id=task_id,build_id=build.build_id,
                    stage_key=stage_key,input_hash=payload_hash)
                sess.add(stage);sess.flush()
                from ..agents.model_routing import TaskModelRoleRow
                pinned=sess.get(TaskModelRoleRow,(root.task_id,"research"))
                scope,_,category=build_domain(sess,sess.get(LearningTaskRow,task_id))
                grant=self.disclosures.derive(parent_grant_id=pinned.grant_id,
                    actor_id=root.owner_user_id,task_id=task_id,config_id=pinned.config_id,
                    config_version=pinned.config_version,content_category=category,scope_snapshot=scope,session=sess)
                pin_role(sess,task_id,"research",{"config_id":pinned.config_id,
                    "config_version":pinned.config_version},grant["grant_id"])
                if work:
                    for role in ("evidence_search","research_search"):
                        search_pin=sess.get(TaskModelRoleRow,(root.task_id,role))
                        if search_pin:
                            search_grant=self.disclosures.derive(parent_grant_id=search_pin.grant_id,
                                actor_id=root.owner_user_id,task_id=task_id,config_id=search_pin.config_id,
                                config_version=search_pin.config_version,content_category=category,
                                scope_snapshot=scope,session=sess)
                            pin_role(sess,task_id,role,{"config_id":search_pin.config_id,
                                "config_version":search_pin.config_version},search_grant["grant_id"])
                sess.add(PromptBindingRow(binding_id="pbind_"+uuid.uuid4().hex,task_id=task_id,
                    prompt_name="nuwa_"+stage_key,source="local_fallback",template_snapshot=prompt,
                    template_sha256=hashlib.sha256(prompt.encode()).hexdigest(),output_schema_version="nuwa-1",
                    agent_code_version="nuwa-1"))
        current=self.tasks.get_task(task_id)
        if current["status"] in ("leased","running") and current["generation"]<root.generation:
            with self.db.session() as sess:
                child=sess.execute(select(LearningTaskRow).where(
                    LearningTaskRow.task_id==task_id).with_for_update()
                    .execution_options(populate_existing=True)).scalar_one_or_none()
                if (child and child.status in ("leased","running")
                        and child.generation<root.generation):
                    self.tasks._terminal(sess,child,status="failed",
                        error_code="TASK_LEASE_EXPIRED",now=datetime.now(timezone.utc))
            current=self.tasks.get_task(task_id)
        if current["status"] in ("failed","expired"):
            self.tasks.requeue_for_retry(task_id,actor_id=root.owner_user_id)
        lease=self.tasks.claim(task_id,lease_owner="persona-"+stage_key[:30],lease_seconds=120,
                               allow_reclaim_active=False)
        began=monotonic()
        adapter=None
        model_metrics={}
        try:
            if work:
                result=work(task_id)
            else:
                adapter=self.model_factory(root.owner_user_id,task_id)
                if not adapter:raise error("MODEL_CAPABILITY_UNAVAILABLE")
                # HTTP read inactivity is separate from the complete 30-minute phase.
                fast=request.get("pipeline_version",1)>=7
                timeout=min({"fast_research":120,"fast_skill":240,"fast_replies":90,
                    "fast_verify":90}.get(stage_key,60),
                    max(1,(utc_deadline(root.deadline_at)-datetime.now(timezone.utc)).total_seconds()-3)) if fast else 1800
                if hasattr(adapter,"timeout"):adapter.timeout=min(60,timeout) if fast else 180
                adapter.separate_connect_timeout=True
                frozen=self.prompts.resolve_for_task(task_id)["template_snapshot"]
                from ..agents.runner import deps_for_source
                from dataclasses import replace
                adapter.agent_deps=replace(deps_for_source(adapter),omit_content_trace=True)
                def validate(ctx,value):
                    from pydantic_ai.exceptions import ModelRetry
                    from .dialogue import DialogueReply
                    if isinstance(value,DialogueReply):
                        if (payload.get("source_text") and payload.get("questions_remaining")==0
                                or payload.get("finish")) and (
                                    value.question or value.reply.rstrip().endswith(("?","？"))):
                            raise ModelRetry("追问已结束，question必须为空，reply中也不再提出问题。")
                        if (value.profile.media_ids or value.profile.voice_config_id or value.profile.voice_id
                                or value.profile.base_asset_id or value.profile.pointing_rig):
                            raise ModelRetry("人物草稿不能生成媒体、音色或配置ID。")
                        if request.get("complete_card") and value.ready:
                            if value.question:
                                raise ModelRetry("信息已明确时reply简要介绍人物，question留空，服务端会追加创作确认问句。")
                            if not all(getattr(value.profile,key).strip() for key in (
                                    "identity","background","personality","appearance")):
                                raise ModelRetry("开始完整生成前须明确身份、背景、性格和外观，可依据用户意图合理设计。")
                        if value.ready or payload.get("finish"):
                            if value.profile.name not in value.reply or len(value.reply)>1200:
                                raise ModelRetry("最终reply须用简短自然段介绍人物，包含profile.name，最多1200字。")
                    if output_type is str and (not isinstance(value,str) or not 200<=len(value)<=48000):
                        raise ModelRetry("完整人物Markdown正文须为200到48000字，不能只返回修订摘要。")
                    if isinstance(value,ResearchReport):
                        available={item["id"] for item in payload.get("sources",[])}
                        if not set(value.source_ids)<=available:
                            raise ModelRetry("source_ids只能使用提供的真实来源ID。")
                    profile=value.profile if isinstance(value,BuiltPersona) else value if isinstance(value,Profile) else None
                    if profile and (profile.media_ids or profile.voice_config_id or profile.voice_id
                            or profile.base_asset_id or profile.pointing_rig):
                        raise ModelRetry("不得生成媒体、音色或私有配置ID。")
                    if isinstance(value,(PersonaExtraction,PersonaTraits)) and unmeasured_style_numbers(value.expression_dna):
                        raise ModelRetry("表达统计未测量，只能保留定性观察，不能返回估计句长或代词比例。")
                    if isinstance(value,PersonaTests) and (
                            [item.kind for item in value.cases].count("known")!=3 or
                            [item.kind for item in value.cases].count("edge")!=1 or
                            [item.kind for item in value.cases].count("voice")!=1):
                        raise ModelRetry("五题须包含三个known、一个edge和一个voice。")
                    if isinstance(value,SourceReview):
                        sources={item["id"]:item for item in payload["sources"]}
                        if {item.source_id for item in value.assessments}!=set(sources) or (
                            len(value.assessments)!=len(sources)):
                            raise ModelRetry("每条来源恰好给出一个分类，不新增或遗漏来源。")
                        for item in value.assessments:
                            checked=verified_assessment(sources[item.source_id],item)
                            item.kind,item.evidence,item.reason=checked["kind"],checked["evidence"],checked["reason"]
                    if isinstance(value,FastResearch):
                        if payload.get("agreed_profile"):
                            value.profile=Profile.model_validate(payload["agreed_profile"])
                        sources={item["id"]:item for item in payload["sources"]}
                        if (len(value.assessments)!=len(sources)
                                or {item.source_id for item in value.assessments}!=set(sources)):
                            raise ModelRetry("每条来源必须恰好审阅一次，不增删来源。")
                        for key in DIMENSIONS:
                            if not set(getattr(value,key).source_ids)<=set(sources):
                                raise ModelRetry("六维原稿只引用输入的来源ID。")
                        if unmeasured_style_numbers(value.extraction.expression_dna):
                            raise ModelRetry("表达特征只做定性观察，不伪造未测量的风格数字。")
                        if (value.profile.media_ids or value.profile.voice_config_id or value.profile.voice_id
                                or value.profile.base_asset_id or value.profile.pointing_rig):
                            raise ModelRetry("不得生成媒体、音色或私有配置ID。")
                    if isinstance(value,SourceAssessment):
                        source=payload.get("source") or {}
                        if value.source_id!=source.get("id"):
                            raise ModelRetry("source_id必须与唯一输入来源一致。")
                        checked=verified_assessment(source,value)
                        for name in ("kind","evidence","reason","relevant","dimensions"):
                            setattr(value,name,checked.get(name,getattr(value,name)))
                    return value
                stream_state=(getattr(adapter,"capability_status",{}) or {}).get("streaming")
                if isinstance(stream_state,dict):stream_state=stream_state.get("state")
                streaming=(getattr(adapter,"supports_text_stream",False) and stream_state!="unavailable"
                    and stage_key not in {"research_plan","coverage_gap_plan","gap_plan"}
                    and not stage_key.startswith("source_review")
                    and not stage_key.startswith("research_writings"))
                runner=run_streaming_structured_agent if streaming else run_structured_agent
                preview=None
                if streaming and stage_key=="dialogue_reply":
                    from .dialogue_stream import DialoguePreviewWriter
                    preview=DialoguePreviewWriter(self,root,lease)
                def on_delta(text,*args,**kwargs):
                    if text and "first_output_ms" not in model_metrics:
                        model_metrics["first_output_ms"]=int((monotonic()-began)*1000)
                    if preview is not None:
                        preview(text,reset=kwargs.get("reset",False))
                options={"on_delta":on_delta,"progress_timeout_seconds":min(60,timeout) if fast else 180} if streaming else {}
                execution=runner(adapter,output_type,[{"role":"system","content":frozen},
                    {"role":"user","content":encode(payload)}],agent_name="nuwa_"+stage_key,
                    output_validator=validate,max_requests=2 if fast else 8,timeout_seconds=timeout,**options)
                result=execution.payload
                model_metrics.update(model_requests=execution.requests,wire_requests=execution.wire_requests)
            data=result.model_dump() if hasattr(result,"model_dump") else result
            def publish(sess):
                self.tasks.validate_lease(root,session=sess)
                build_domain(sess,sess.get(LearningTaskRow,task_id))
                row=sess.get(CharacterBuildStageRow,task_id)
                row.result_json=encode(data)
                row.state_json=encode({**json.loads(row.state_json or "{}"),**model_metrics,
                    "duration_ms":int((monotonic()-began)*1000)})
            receipt=self.tasks.submit_result(lease,stage="search" if work else "generate",
                status="succeeded",publish=publish,duration_ms=int((monotonic()-began)*1000))
            if not receipt.get("accepted"):raise error("STALE_RESULT_DISCARDED")
            return result
        except Exception as exc:
            with self.db.session() as sess:
                row=sess.get(CharacterBuildStageRow,task_id)
                row.state_json=encode({**json.loads(row.state_json or "{}"),**model_metrics,
                    "duration_ms":int((monotonic()-began)*1000),"error_code":self._code(exc)})
            failed=self.tasks.submit_result(lease,stage="search" if work else "generate",status="failed",
                error_code=self._code(exc),duration_ms=int((monotonic()-began)*1000))
            if not failed.get("accepted"):
                self.tasks.recover_worker_failure(lease,stage="search" if work else "generate",error_code=self._code(exc))
            if (request.get("pipeline_version",1)>=6 and stage_key in ("coverage_gap_plan","gap_plan")
                    and self._code(exc) in {"MODEL_TIMEOUT","MODEL_CONNECTION_ERROR","MODEL_PROVIDER_ERROR","OUTPUT_INVALID"}):
                self._stage(root,stage_key+"_fallback",{"error_code":self._code(exc)},None,prompts.COMMON,
                    work=lambda task:{"supplement":"unavailable","error_code":self._code(exc)})
                return None
            raise
        finally:
            if adapter and hasattr(adapter,"close"):adapter.close()

    @staticmethod
    def _code(exc):
        from ..agents.limits import to_controlled_error
        from ..logging_audit.contracts import normalize_error_code
        from ..tasks.service import LeaseError
        if isinstance(exc,LeaseError):
            return normalize_error_code(exc.code)
        try:
            return to_controlled_error(exc).code
        except Exception:
            return normalize_error_code(getattr(exc,"code","INTERNAL_ERROR"))

    def _sources(self,root,request):
        if request.get("pipeline_version",1)>=6 and request.get("reuse_build_id") and self._source_pool(root):
            self._stage(root,"collection_plan",{"reused":request.get("reuse_sources_sha256")},
                None,prompts.COMMON,work=lambda task:{"initial_count":0,"initial_read_budget":0,
                    "reserved_gap_reads":request.get("research_policy",{}).get("max_reads",40)})
            return self._stage(root,"source_snapshot",{"round":0},None,prompts.COMMON,
                work=lambda task:{"sources":self._source_pool(root)})["sources"]
        if request.get("pipeline_version",1)>=2:
            plan=self._stage(root,"research_plan",{"topic":request["topic"],"as_of":self._as_of(root)},
                ResearchPlan,prompts.PLAN)
            policy=ResearchPolicy.model_validate(request.get("research_policy",{}))
            count=min(6,policy.max_searches,len(plan.queries))
            per_query_reads=self._initial_read_budget(policy,count)
            if request.get("pipeline_version",1)>=3:
                collection=self._stage(root,"collection_plan",{"plan":plan.model_dump()},None,prompts.COMMON,
                    work=lambda task:{"initial_count":0 if request.get("reuse_build_id") and self._source_pool(root)
                        else count,
                        "initial_read_budget":0 if request.get("reuse_build_id") and self._source_pool(root)
                        else per_query_reads,
                        "reserved_gap_reads":max(0,policy.max_reads-per_query_reads*count)
                        if not request.get("reuse_build_id") or not self._source_pool(root) else policy.max_reads})
                count=collection["initial_count"]
                per_query_reads=collection.get("initial_read_budget",per_query_reads)
            self._collect_queries(root,request,plan.queries[:count],0,
                per_query_reads=per_query_reads or None)
            return self._stage(root,"source_snapshot",{"round":0},None,prompts.COMMON,
                work=lambda task_id:{"sources":self._source_pool(root)})["sources"]
        def collect(task_id):
            with self.db.session() as sess:
                row=sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id==root.task_id).with_for_update())
                if row.sources_json!="[]":return {"sources":json.loads(row.sources_json)}
                if row.search_started:
                    raise error("SEARCH_OUTPUT_INVALID","search response unavailable; budget is not reset")
                row.search_started=True
            if not self.search_factory:raise error("SEARCH_CONNECTION_ERROR")
            search=self.search_factory(root.owner_user_id,task_id)
            outcome=search.attempt(request["topic"],max_results=10)
            if outcome.state!="succeeded":raise error(getattr(outcome,"error_code","SEARCH_OUTPUT_INVALID"))
            sources=[{"id":f"source_{index}","url":item.url,"title":item.title,"text":item.excerpt,
                      "kind":"unverified","fetched_at":item.fetched_at}
                for index,item in enumerate(outcome.results,1) if self._allowed_source(item.url)]
            for source in sources:
                try:
                    content=self._read_source(root,source["url"])
                    source["text"]=content or source["text"]
                    source["read_status"]="full_text" if content else "excerpt_only"
                except Exception:source["read_status"]="unavailable"
            if not sources:raise error("SEARCH_OUTPUT_INVALID","no permitted sources")
            with self.db.session() as sess:
                build_domain(sess,sess.get(LearningTaskRow,task_id))
                row=sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id==root.task_id))
                row.sources_json=encode(sources)
            return {"sources":sources}
        return self._stage(root,"sources",{"topic":request["topic"]},None,prompts.COMMON,work=collect)["sources"]

    def _as_of(self,root):
        with self.db.session() as sess:
            return sess.scalar(select(CharacterBuildRow).where(
                CharacterBuildRow.task_id==root.task_id)).created_at.date().isoformat()

    def _source_pool(self,root):
        with self.db.session() as sess:
            return json.loads(sess.scalar(select(CharacterBuildRow).where(
                CharacterBuildRow.task_id==root.task_id)).sources_json)

    def _completed_stage(self,root,key,output_type):
        with self.db.session() as sess:
            build=sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id==root.task_id))
            stage=sess.scalar(select(CharacterBuildStageRow).where(
                CharacterBuildStageRow.build_id==build.build_id,CharacterBuildStageRow.stage_key==key))
            if stage and stage.result_json!="{}":
                return output_type.model_validate_json(stage.result_json)
        return None

    @staticmethod
    def _next_search_slot(stages):
        indexes=[]
        for stage in stages:
            if not stage.stage_key.startswith("sources_"):
                continue
            try:indexes.append(int(stage.stage_key.split("_",1)[1]))
            except (TypeError,ValueError):continue
        return max(indexes,default=-1)+1

    @staticmethod
    def _initial_read_budget(policy,count):
        if count<=0:return 0
        # Keep a small reserve for evidence-driven gap searches instead of
        # exhausting the read budget in the first batch.
        reserve=min(8,max(4,policy.max_reads//5))
        return max(1,(policy.max_reads-reserve)//count)

    def _collect_queries(self,root,request,queries,offset,*,per_query_reads=None):
        policy=ResearchPolicy.model_validate(request.get("research_policy",{}))
        recoverable={"MODEL_TIMEOUT","MODEL_CONNECTION_ERROR","MODEL_PROVIDER_ERROR",
                     "OUTPUT_INVALID","AGENT_LOOP_LIMIT"}
        for index,item in enumerate(queries,offset):
            if index>=policy.max_searches:break
            query_read_budget=per_query_reads
            if query_read_budget is None:
                with self.db.session() as sess:
                    build=sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id==root.task_id))
                    states=[json.loads(stage.state_json or "{}") for stage in sess.scalars(
                        select(CharacterBuildStageRow).where(CharacterBuildStageRow.build_id==build.build_id))]
                reads_used=sum(state.get("read_attempts",0) for state in states)
                slots_left=max(1,policy.max_searches-index)
                reads_left=max(0,policy.max_reads-reads_used)
                query_read_budget=max(1,reads_left//slots_left) if reads_left else 0
            query=" ".join(item.query.split())
            if request["topic"].casefold() not in query.casefold():
                query=request["topic"]+" "+query
            query=query[:400]
            try:
                self._stage(root,f"sources_{index}",{"query":query,"dimensions":item.dimensions},
                    None,prompts.COMMON,work=lambda task_id,q=query,d=item.dimensions,rb=query_read_budget:
                        self._collect_query(root,task_id,q,d,request,policy,rb))
            except Exception as exc:
                code=self._code(exc)
                if code not in recoverable:raise
                self._stage(root,f"sources_{index}_fallback",{"query":query,"error_code":code},
                    None,prompts.COMMON,
                    work=lambda task_id,c=code:{"search_status":"unknown","error_code":c})

    def _collect_query(self,root,task_id,query,dimensions,request,policy,per_query_reads=None):
        from ..agents.model_routing import TaskModelRoleRow
        with self.db.session() as sess:
            self.tasks.validate_lease(root,session=sess)
            stage=sess.get(CharacterBuildStageRow,task_id,with_for_update=True)
            state=json.loads(stage.state_json)
            if state.get("search_started"):
                stage.state_json=encode({**state,"interrupted":True})
                return {"interrupted":True,"sources":[]}
            build=sess.get(CharacterBuildRow,stage.build_id)
            stages=list(sess.scalars(select(CharacterBuildStageRow).where(
                CharacterBuildStageRow.build_id==build.build_id)))
            states=[json.loads(row.state_json or "{}") for row in stages]
            blocked=next((item["error_code"] for item in states if item.get("error_code") in (
                "MODEL_CREDENTIAL_INVALID","SEARCH_CREDENTIAL_INVALID","SEARCH_QUOTA_EXCEEDED",
                "MODEL_RATE_LIMITED","SEARCH_RATE_LIMITED","AGENT_LOOP_LIMIT")),None)
            if blocked:raise error(blocked)
            if (sum(row.get("read_attempts",0) for row in states)>=policy.max_reads
                    or sum(row.get("candidate_count",0) for row in states)>=policy.max_candidates):
                return {"sources":[],"budget_exhausted":True}
            if sum(bool(row.get("search_started")) for row in states)>=policy.max_searches:
                return {"sources":[],"budget_exhausted":True}
            if any(row.get("query")==query and row.get("search_started") for row in states):
                return {"sources":[],"duplicate_query":True}
            stage.state_json=encode({"query":query,"search_started":True,
                "read_attempts":0,"candidate_count":0,
                "read_budget":policy.max_reads if per_query_reads is None else per_query_reads})
        # Intent is committed before the paid request. A lost response never resets the slot.
        if request.get("search_provider")=="grok":
            from .research_search import configured_grok_search
            result=configured_grok_search(self,root,task_id,query,
                max_tool_calls=1 if request.get("dialogue") else 3)
            candidates=result["results"]
            metrics=result["metrics"]
        else:
            if not self.search_factory:raise error("SEARCH_CONNECTION_ERROR")
            search=self.search_factory(root.owner_user_id,task_id)
            with self.db.session() as sess:
                pinned=sess.get(TaskModelRoleRow,(root.task_id,"evidence_search"))
                if pinned and hasattr(search,"_view"):
                    search._view=self.cards.configs.get(pinned.config_id,actor_id=root.owner_user_id,is_admin=False)
            outcome=search.attempt(query,max_results=10)
            if outcome.state not in ("succeeded","empty"):
                raise error(getattr(outcome,"error_code","SEARCH_OUTPUT_INVALID"))
            candidates=[{"url":item.url,"title":item.title,"excerpt":item.excerpt}
                for item in getattr(outcome,"results",[])]
            metrics={"completed_actions":1,"actions":{"search":1},"visible_urls":len(candidates)}
        with self.db.session() as sess:
            self.tasks.validate_lease(root,session=sess)
            stage=sess.get(CharacterBuildStageRow,task_id,with_for_update=True)
            states=[json.loads(row.state_json or "{}") for row in sess.scalars(
                select(CharacterBuildStageRow).where(CharacterBuildStageRow.build_id==stage.build_id))]
            used_candidates=sum(row.get("candidate_count",0) for row in states)
            candidates=candidates[:max(0,policy.max_candidates-used_candidates)]
            stage.state_json=encode({**json.loads(stage.state_json),"candidate_count":len(candidates),
                "search_metrics":metrics})
        collected=[]
        for candidate in candidates:
            url=canonical_source(candidate["url"])
            pool=self._source_pool(root)
            if not url or not self._allowed_source(url) or any(source.get("canonical_url")==url for source in pool):
                continue
            with self.db.session() as sess:
                self.tasks.validate_lease(root,session=sess)
                stage=sess.get(CharacterBuildStageRow,task_id,with_for_update=True)
                reads=sum(json.loads(row.state_json or "{}").get("read_attempts",0) for row in sess.scalars(
                    select(CharacterBuildStageRow).where(CharacterBuildStageRow.build_id==stage.build_id)))
                state=json.loads(stage.state_json)
                if state["read_attempts"]>=state.get("read_budget",policy.max_reads):break
                if reads>=policy.max_reads:break
                stage.state_json=encode({**state,"read_attempts":state["read_attempts"]+1})
            source={"id":"source_"+digest(url)[:16],"url":candidate["url"],"canonical_url":url,
                "title":candidate["title"],"text":candidate.get("excerpt","")[:2000],
                "kind":"unknown","dimensions":[],"requested_dimensions":dimensions,"usable":False}
            try:
                content=self._read_source(root,candidate["url"])
                status=page_status(content)
                source.update(text=content or source["text"],
                    read_status=("partial_text" if len(content)>=16000 else "full_text")
                        if status=="readable" else status,
                    capture_truncated=len(content)>=16000)
            except Exception as exc:
                from ..tasks.service import LeaseError
                if isinstance(exc,LeaseError) or getattr(exc,"code",None) in ("TASK_CANCELLED","DISCLOSURE_REVOKED","AGENT_LOOP_LIMIT"):
                    raise
                source["read_status"]="unavailable"
            source["body_sha256"]=body_hash(source["text"])
            if any(existing.get("read_status") in ("full_text","partial_text") and existing.get("body_sha256")==source["body_sha256"]
                    for existing in pool):
                source.update(read_status="duplicate_body",text="",usable=False)
            with self.db.session() as sess:
                self.tasks.validate_lease(root,session=sess)
                build_domain(sess,sess.get(LearningTaskRow,task_id))
                build=sess.scalar(select(CharacterBuildRow).where(
                    CharacterBuildRow.task_id==root.task_id).with_for_update())
                pool=json.loads(build.sources_json)
                if not any(existing["id"]==source["id"] for existing in pool):
                    pool.append(source)
                    build.sources_json=encode(pool)
            collected.append(source["id"])
        return {"sources":collected,"search_metrics":metrics}

    def _review_sources(self,root,request,sources,key):
        if not sources:return []
        assessments={}
        candidates=sources
        if request.get("pipeline_version",1)>=3:
            candidates=[source for source in sources if source.get("read_status") in ("full_text","partial_text")]
        recoverable={"MODEL_TIMEOUT","MODEL_CONNECTION_ERROR","MODEL_PROVIDER_ERROR","OUTPUT_INVALID"}
        if request.get("pipeline_version",1)>=3:
            def review_one(source):
                review_source={**source,"text":source.get("text","")[:4000],
                    "context_truncated":len(source.get("text",""))>4000}
                payload={"topic":request["topic"],"source":review_source}
                batch_key=key+"_"+digest(source["id"])[:12]
                prior_error=None
                with self.db.session() as sess:
                    build=sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id==root.task_id))
                    prior=sess.scalar(select(CharacterBuildStageRow).where(
                        CharacterBuildStageRow.build_id==build.build_id,CharacterBuildStageRow.stage_key==batch_key))
                    if (prior and prior.input_hash==digest(stage_payload(request,payload))
                            and sess.get(LearningTaskRow,prior.task_id).status=="failed"):
                        prior_error=json.loads(prior.state_json or "{}").get("error_code")
                try:
                    if prior_error in recoverable:raise error(prior_error)
                    review=self._stage(root,batch_key,payload,SourceAssessment,
                        prompts.SOURCE_REVIEW_SINGLE)
                except Exception as exc:
                    code=self._code(exc)
                    if code not in recoverable:raise
                    self._stage(root,batch_key+"_fallback",{"source_id":source["id"],
                        "error_code":code},None,prompts.COMMON,
                        work=lambda task,c=code:{"classification":"unknown","error_code":c})
                    return None
                return review
            if request.get("pipeline_version",1)>=6:
                with ThreadPoolExecutor(max_workers=3) as workers:
                    pending=[workers.submit(copy_context().run,review_one,source) for source in candidates]
                    reviews=[future.result() for future in pending]
            else:
                reviews=[review_one(source) for source in candidates]
            assessments={review.source_id:review for review in reviews if review is not None}
        else:
            batch_size=8
            for offset in range(0,len(candidates),batch_size):
                batch_key=key+"_"+str(offset//batch_size)
                payload={"topic":request["topic"],"sources":candidates[offset:offset+batch_size]}
                review=self._stage(root,batch_key,payload,SourceReview,prompts.SOURCE_REVIEW)
                assessments.update({item.source_id:item for item in review.assessments})
        reviewed=[verified_assessment(source,assessments[source["id"]]) if source["id"] in assessments else
            {**source,"kind":"unknown","evidence":"","dimensions":[],"usable":False,
             "reason":"未读到可用正文，不交由模型推定来源。"} for source in sources]
        for source in reviewed:
            if source["id"] in {item["id"] for item in candidates} and source["id"] not in assessments:
                source["reason"]="来源分类未完成，保留未知且不计有效证据。"
        with self.db.session() as sess:
            self.tasks.validate_lease(root,session=sess)
            build_domain(sess,sess.get(LearningTaskRow,root.task_id))
            build=sess.scalar(select(CharacterBuildRow).where(
                CharacterBuildRow.task_id==root.task_id).with_for_update())
            values={source["id"]:source for source in reviewed}
            build.sources_json=encode([values.get(source["id"],source) for source in json.loads(build.sources_json)])
        return reviewed

    @staticmethod
    def _source_context(sources,limit=32000,dimension=None,balanced=False):
        selected=sorted((source for source in sources if source.get("usable",True)
            and (dimension is None or dimension in source.get("dimensions",[])
                 or not source.get("dimensions") and dimension in source.get("requested_dimensions",[]))),
            key=lambda source:(
            not source.get("usable",True),
            bool(dimension and dimension not in source.get("dimensions",[]))))
        result=[]
        remaining=limit
        for index,source in enumerate(selected):
            if remaining<=0:break
            text=source.get("text","")
            take=min(len(text),remaining,4000 if dimension else len(text))
            offset=0
            if balanced:
                take=min(take,max(1,remaining//(len(selected)-index)))
                offset=max(0,min(len(text)-take,(source.get("evidence_start") or 0)-take//3))
            result.append({**{key:source.get(key) for key in (
                "id","kind","dimensions","read_status","usable","body_sha256")},
                "title":source.get("title","")[:160],
                "url":source.get("url","") if len(source.get("url",""))<=500 else "",
                "text":text[offset:offset+take],
                **({"text_offset":offset,"verified_quote":source.get("evidence","")} if balanced else {}),
                "context_truncated":take<len(text)})
            remaining-=take
        return result

    def _research_reports(self,root,subject,keys=DIMENSIONS,suffix="",legacy=False,
                          allow_partial=False):
        reports={}
        failures={}
        with ThreadPoolExecutor(max_workers=3) as workers:
            futures={key:workers.submit(copy_context().run,self._stage,root,
                "research_"+key+("_bounded3" if not legacy else "")+suffix,
                {**subject,"sources":subject["sources"] if legacy else self._source_context(subject["sources"],dimension=key),"dimension":key},
                ResearchReport,prompts.RESEARCH+prompts.dimensions_for(
                    subject.get("character_subject",{}))[key][1]) for key in keys}
            for key,future in futures.items():
                try:
                    reports[key]=future.result().model_dump()
                except Exception as exc:
                    code=self._code(exc)
                    if not allow_partial or code not in {
                        "MODEL_TIMEOUT","MODEL_CONNECTION_ERROR","MODEL_PROVIDER_ERROR","OUTPUT_INVALID"}:
                        raise
                    failures[key]=code
                    reports[key]={"body":"该维度研究未完成，不能把模型推断当作事实原稿。",
                        "source_ids":[],"gaps":[f"研究阶段失败：{code}"],"contradictions":[]}
        return (reports,failures) if allow_partial else reports

    @staticmethod
    def _report_context(reports,limit=6000):
        """Keep full versioned reports in checkpoints; bound each downstream prompt."""
        return {key:{**report,"body":report.get("body","")[:limit],
                     "context_truncated":len(report.get("body",""))>limit}
                for key,report in reports.items()}

    @staticmethod
    def _allowed_source(url):
        from urllib.parse import urlsplit
        host=(urlsplit(url).hostname or "").lower()
        return bool(host) and not any(host==domain or host.endswith("."+domain)
            for domain in (
                "zhihu.com","mp.weixin.qq.com","baike.baidu.com","youtube.com","youtu.be",
                "linkedin.com","threads.net","twitter.com","x.com","facebook.com",
                "instagram.com","coursehero.com","scribd.com","books.google.com"))

    def _read_source(self,root,url):
        """Read only a selected public HTTPS page with pinned DNS and no redirects."""
        import asyncio
        from ..agents.contracts import AgentDeps
        from .evidence import read_public_source
        if not self._allowed_source(url):return ""
        def check():
            with self.db.session() as sess:
                self.tasks.validate_lease(root,session=sess)
                build_domain(sess,sess.get(LearningTaskRow,root.task_id))
        return asyncio.run(read_public_source(url,AgentDeps(check_state=check)))

    def run(self,ident):
        root=None
        try:
            with self.db.session() as sess:
                row=sess.get(CharacterBuildRow,ident)
                if not row:return
                owner,task_id=row.owner_user_id,row.task_id
            task=self.tasks.get_task(task_id)
            if task["status"] in ("succeeded","cancelled"):return task
            self._ensure_execution_budget(owner,ident)
            if task["status"] in ("failed","expired"):
                with self.db.session() as sess:
                    version=json.loads(sess.get(CharacterBuildRow,ident).request_json).get("pipeline_version",1)
                self.tasks.requeue_for_retry(task_id,actor_id=owner,deadline_seconds=600 if version>=7 else 7200)
            root=self.tasks.claim(task_id,lease_owner="persona-build",lease_seconds=120,allow_reclaim_active=False)
            with self.db.session() as sess:
                row=sess.get(CharacterBuildRow,ident);request=json.loads(row.request_json)
            with ThreadPoolExecutor(max_workers=1) as worker:
                future=worker.submit(copy_context().run,self._pipeline,root,request)
                while True:
                    try:result=future.result(timeout=25);break
                    except TimeoutError:
                        if future.done():
                            result=future.result()
                            break
                        self.tasks.heartbeat(root,lease_seconds=120)
                        self._renew_stages(ident,owner)
            def publish(sess):
                build_domain(sess,sess.get(LearningTaskRow,root.task_id))
                row=sess.get(CharacterBuildRow,ident)
                if result.get("completion",{}).get("status")=="complete":
                    from .creation_delivery import confirm_assets
                    confirm_assets(self,sess,root,request,result)
                    self._accept_result(sess,row,result)
                row.result_json=encode(result)
            return self.tasks.submit_result(root,stage="generate",status="succeeded",publish=publish)
        except Exception as exc:
            if root:
                failed=self.tasks.submit_result(root,stage="generate",status="failed",error_code=self._code(exc))
                if not failed.get("accepted"):
                    self.tasks.recover_worker_failure(root,stage="generate",error_code=self._code(exc))
            return {"status":"failed","error_code":self._code(exc)}
        finally:
            with self.lock:self.running.discard(ident)

    def _ensure_execution_budget(self,owner,ident):
        with self.db.session() as sess:
            row=self._owned(sess,owner,ident)
            if json.loads(row.request_json).get("pipeline_version",1)<6:return
            budget=sess.get(AgentBudgetRow,row.task_id,with_for_update=True)
            if budget and budget.request_limit==128:
                # Early v6 builds inherited the shorter v5 workflow ceiling.
                # Upgrade its ceiling once, without resetting any consumption.
                budget.request_limit=256
                self.cards.audit.append("config.changed",actor_id=owner,object_type="character_build",
                    object_id=ident,result="updated",session=sess)

    def _renew_stages(self,ident,owner):
        from ..tasks.service import Lease,LeaseError
        with self.db.session() as sess:
            stages=list(sess.scalars(select(CharacterBuildStageRow).where(
                CharacterBuildStageRow.build_id==ident)))
            leases=[]
            for stage in stages:
                task=sess.get(LearningTaskRow,stage.task_id)
                if task.status in ("running","leased"):
                    leases.append(Lease(task.task_id,task.attempt_id,task.lease_token,task.lease_owner,
                        task.generation,task.attempt,task.lease_until,task.deadline_at,owner))
        for lease in leases:
            try:self.tasks.heartbeat(lease,lease_seconds=120)
            except LeaseError:
                # A stage may finish between the snapshot and renewal.
                if self.tasks.get_task(lease.task_id)["status"] not in ("succeeded","failed","cancelled","expired"):
                    raise

    def _pipeline(self,root,request):
        if request.get("dialogue"):
            from .dialogue import run_dialogue
            result=run_dialogue(self,root,request)
            if request.get("complete_card") and result.get("persona"):
                from .creation_delivery import complete_card
                result=complete_card(self,root,request,result)
            return result
        if request.get("pipeline_version",1)>=7:
            from .fast_distillation import create_persona
            return create_persona(self,root,request)
        legacy=request.get("pipeline_version",1)<2
        graded=request.get("pipeline_version",1)>=6
        character_subject=request.get("subject",{})
        original=graded and character_subject.get("kind")=="original"
        with self.db.session() as sess:
            build=sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id==root.task_id))
            as_of=build.created_at.date().isoformat()
        if graded:
            bundle=self._research_bundle(root,restore=True)
            if bundle is not None:
                return self._compose(root,request,bundle["sources"],bundle["reports"],
                    bundle["research_failures"],as_of)
        if original:
            sources=[{"id":"author_setting","title":request["topic"],
                "text":request["topic"]+"\n"+request["purpose"]+"\n"+request.get("source_text",""),
                "kind":"user_material","url":"","usable":True,"dimensions":[],
                "requested_dimensions":list(DIMENSIONS)}]
            with self.db.session() as sess:
                self.tasks.validate_lease(root,session=sess)
                sess.scalar(select(CharacterBuildRow).where(
                    CharacterBuildRow.task_id==root.task_id)).sources_json=encode(sources)
        elif request["mode"]=="text":
            if not request["selected_character"]:
                candidates=self._stage(root,"candidates",{"text":request["source_text"]},
                    CharacterCandidates,prompts.COMMON+"找出材料中的可蒸馏人物名称，保留原名，不创造人物。")
                if len(candidates.candidates)>1:return {"candidates":candidates.candidates}
                request={**request,"selected_character":candidates.candidates[0]}
            sources=[{"id":"uploaded_txt","title":request["selected_character"],"text":request["source_text"],
                      "kind":"user_material","url":""}]
            if graded:
                sources[0].update(usable=True,dimensions=[],requested_dimensions=list(DIMENSIONS))
        else:
            sources=self._sources(root,request)
            if request.get("pipeline_version",1)>=2:
                review_key=("source_review_compact" if request.get("pipeline_version",1)>=3
                    else "source_review")
                sources=self._review_sources(root,request,sources,review_key)
            else:
                review=self._stage(root,"source_review",{"topic":request["topic"],"sources":sources},
                                   SourceReview,prompts.SOURCE_REVIEW)
                assessment={item.source_id:item for item in review.assessments}
                sources=[{**item,**assessment[item["id"]].model_dump(
                    exclude={"source_id","relevant","dimensions"})} for item in sources]
        subject={"topic":request["selected_character"] or request["topic"],"purpose":request["purpose"],
                 "sources":sources,"as_of":as_of}
        if graded:subject["character_subject"]=character_subject
        policy=ResearchPolicy.model_validate(request.get("research_policy",{}))
        research_failures={}
        if request["mode"]=="ai" and request.get("pipeline_version",1)>=2 and not original:
            # Run a first coverage pass before the dimension agents so missing
            # evidence is not held hostage by a later model timeout.
            coverage=evidence_summary(sources)
            if graded:self._evidence_round(root,"initial",sources)
            needs_coverage=(coverage["effective_documents"]==0 if graded else
                not all(coverage["dimensions"].values()) or coverage["effective_documents"]<policy.min_documents
                    or coverage["verified_primary"]<policy.min_primary)
            if needs_coverage:
                with self.db.session() as sess:
                    build=sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id==root.task_id))
                    stages=list(sess.scalars(select(CharacterBuildStageRow).where(
                        CharacterBuildStageRow.build_id==build.build_id)))
                    states=[json.loads(stage.state_json or "{}") for stage in stages]
                    previous=[state.get("query") for state in states if state.get("query")]
                    collection=sess.scalar(select(CharacterBuildStageRow).where(
                        CharacterBuildStageRow.build_id==build.build_id,
                        CharacterBuildStageRow.stage_key=="collection_plan"))
                    next_slot=self._next_search_slot(stages)
                remaining=max(0,policy.max_searches-next_slot)
                if remaining:
                    plan=self._completed_stage(root,"coverage_gap_plan",ResearchPlan)
                    if plan is None:
                        plan=self._stage(root,"coverage_gap_plan",{"topic":request["topic"],
                            "gaps":{"missing_dimensions":[key for key,value in coverage["dimensions"].items() if not value],
                                "effective_documents":coverage["effective_documents"],
                                "effective_shortfall":max(0,policy.min_documents-coverage["effective_documents"]),
                                "primary_shortfall":max(0,policy.min_primary-coverage["verified_primary"])},
                            "coverage":coverage,"previous_queries":sorted(set(previous))},
                            ResearchPlan,prompts.PLAN)
                    self._collect_queries(root,request,plan.queries[:remaining] if plan else [],next_slot)
                    pool=self._stage(root,"source_snapshot_coverage",{"round":"coverage"},None,prompts.COMMON,
                        work=lambda task_id:{"sources":self._source_pool(root)})["sources"]
                    new=[source for source in pool if source["id"] not in {item["id"] for item in sources}]
                    reviewed=self._review_sources(root,request,new,"source_review_coverage_compact")
                    sources+=reviewed
                    with self.db.session() as sess:
                        self.tasks.validate_lease(root,session=sess)
                        build=sess.scalar(select(CharacterBuildRow).where(
                            CharacterBuildRow.task_id==root.task_id).with_for_update())
                        build.sources_json=encode(sources)
                    subject={**subject,"sources":sources}
                    if graded:self._evidence_round(root,"coverage",sources)
        if request["mode"]=="ai" and request.get("pipeline_version",1)>=2:
            reports,research_failures=self._research_reports(root,subject,allow_partial=True)
        else:
            reports=self._research_reports(root,subject,legacy=legacy or request["mode"]=="text")
        if request["mode"]=="ai" and request.get("pipeline_version",1)>=2 and not original:
            checks=evidence_checks(sources,policy)
            needs_more=any(report.get("gaps") for report in reports.values()) or (
                not graded and not all(check["passed"] for check in checks))
            if needs_more and (not graded or not self._search_stalled(root)):
                with self.db.session() as sess:
                    build=sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id==root.task_id))
                    checkpoints={stage.stage_key:stage for stage in sess.scalars(
                        select(CharacterBuildStageRow).where(CharacterBuildStageRow.build_id==build.build_id))}
                    previous=[json.loads(stage.state_json or "{}").get("query")
                        for stage in checkpoints.values()
                        if stage.stage_key.startswith("sources_") and json.loads(
                            stage.state_json or "{}").get("query")]
                    next_slot=self._next_search_slot(list(checkpoints.values()))
                remaining=max(0,policy.max_searches-next_slot)
                if remaining>0:
                    plan=self._completed_stage(root,"gap_plan",ResearchPlan)
                    if plan is None:
                        plan=self._stage(root,"gap_plan",{"topic":request["topic"],
                            "gaps":{key:value.get("gaps",[]) for key,value in reports.items()},
                        "coverage":evidence_summary(sources),
                        "quality_shortfalls":{"effective_documents":max(0,policy.min_documents-
                            evidence_summary(sources)["effective_documents"]),
                            "verified_primary":max(0,policy.min_primary-
                            evidence_summary(sources)["verified_primary"])},
                        "previous_queries":sorted(set(previous))},
                            ResearchPlan,prompts.PLAN)
                    self._collect_queries(root,request,plan.queries[:remaining] if plan else [],next_slot)
                    pool=self._stage(root,"source_snapshot_1",{"round":1},None,prompts.COMMON,
                        work=lambda task_id:{"sources":self._source_pool(root)})["sources"]
                    new=[source for source in pool if source["id"] not in {item["id"] for item in sources}]
                    reviewed=self._review_sources(root,request,new,"source_review_gap_compact")
                    sources+=reviewed
                    affected={key for source in reviewed if source.get("usable")
                        for key in source.get("dimensions",[])}
                    if affected:
                        updated,failed=self._research_reports(root,{**subject,"sources":sources},
                            [key for key in DIMENSIONS if key in affected or key in research_failures],
                            "_updated",allow_partial=True)
                        for key,report in updated.items():
                            if key not in failed or key in research_failures:
                                reports[key]=report
                        research_failures={**research_failures,**{
                            key:code for key,code in failed.items() if key in research_failures}}
                        for key in updated:
                            if key not in failed:research_failures.pop(key,None)
            with self.db.session() as sess:
                build=sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id==root.task_id))
                build.sources_json=encode(sources)
        if graded:
            self._research_bundle(root,bundle={"sources":sources,"reports":reports,
                "research_failures":research_failures})
        return self._compose(root,request,sources,reports,research_failures,as_of)

    def _compose(self,root,request,sources,reports,research_failures,as_of):
        legacy=request.get("pipeline_version",1)<2
        graded=request.get("pipeline_version",1)>=6
        character_subject=request.get("subject",{})
        policy=ResearchPolicy.model_validate(request.get("research_policy",{}))
        subject={"topic":request.get("selected_character") or request["topic"]}
        with self.db.session() as sess:
            build=sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id==root.task_id))
        report_context=self._report_context(reports)
        source_checks=evidence_checks(sources,policy)
        source_index=[{key:value for key,value in item.items() if key!="text"} for item in sources]
        if not legacy:
            source_index=[{key:value for key,value in item.items() if key in (
                "id","kind","read_status","dimensions","body_sha256","evidence_start","evidence_end")}
                |{"url":item.get("url","") if len(item.get("url",""))<=500 else "",
                  "title":item.get("title","")[:160]} for item in sources]
        if graded:
            payload={"reports":self._report_context(reports,2400),
                "source_index":[{"id":item["id"],"kind":item["kind"]} for item in sources],
                "sources":self._source_context(sources,16000,balanced=True),"purpose":request["purpose"]}
            with ThreadPoolExecutor(max_workers=2) as workers:
                models=workers.submit(copy_context().run,self._stage,root,"extract_models_v6",payload,
                    PersonaModels,prompts.EXTRACT+
                    "\n本阶段只提炼models。通常2至4个有依据的机制，每个机制和限制简明具体，证据写两个场景及来源ID。"
                    "没有依据不凑数，完整场景细节已经保留在研究原稿。")
                traits=workers.submit(copy_context().run,self._stage,root,"extract_traits_v6",payload,
                    PersonaTraits,prompts.EXTRACT+
                    "\n本阶段只提炼heuristics、expression_dna、values_and_tensions、boundaries、influences。"
                    "使用具体条件、行动和证据边界，不输出models，不重复研究报告。表达只能定性。")
                extraction=PersonaExtraction.model_validate({
                    **models.result().model_dump(),**traits.result().model_dump()})
        else:
            extraction=self._stage(root,"extract_compact_v2" if request["mode"]=="ai" and
                request.get("pipeline_version",1)>=2 else "extract",{"reports":report_context,
                "source_index":source_index if legacy else [{"id":item["id"],"kind":item["kind"]} for item in sources],
                "sources":sources if legacy else self._source_context(sources),
                "purpose":request["purpose"]},PersonaExtraction,prompts.EXTRACT)
        if request.get("pipeline_version",1)>=4:
            profile=self._stage(root,"build_profile",{"extraction":extraction.model_dump(),
                "topic":subject["topic"],"source_index":source_index},Profile,prompts.BUILD_PROFILE)
            if request.get("pipeline_version",1)>=5:
                def build_part(index,part):
                    name,instruction=part
                    return self._stage(root,f"build_skill_part_{index}",{
                        "part":name,"extraction":extraction.model_dump(),"reports":report_context,
                        "topic":subject["topic"],"as_of":as_of,"source_index":source_index,
                        "profile":profile.model_dump()},
                        str,prompts.COMMON+instruction+
                        "角色身份与证据边界始终优先；正文使用具体可执行的中文Markdown规则。"
                        "输出本段正文不少于200字，不输出JSON或其它章节。")
                if graded:
                    with ThreadPoolExecutor(max_workers=3) as workers:
                        pending=[workers.submit(copy_context().run,build_part,index,part)
                            for index,part in enumerate(prompts.BUILD_PARTS)]
                        parts=[future.result() for future in pending]
                else:
                    parts=[build_part(index,part) for index,part in enumerate(prompts.BUILD_PARTS)]
                assembled=self._stage(root,"build_skill_assembled",{
                    "parts":[hashlib.sha256(part.encode()).hexdigest() for part in parts]},
                    None,prompts.COMMON,work=lambda task_id:{"body":"\n\n".join(parts)})
                skill=assembled["body"]
            else:
                skill=self._stage(root,"build_skill",{"extraction":extraction.model_dump(),"reports":report_context,
                    "topic":subject["topic"],"source_index":source_index,"profile":profile.model_dump()},
                    str,prompts.BUILD+"\n只返回完整Markdown正文，不返回JSON；展示profile已单独保存。")
            candidate=BuiltPersona(profile=profile,skill=skill)
        else:
            candidate=self._stage(root,"build",{"extraction":extraction.model_dump(),"reports":report_context,
                "topic":subject["topic"],"source_index":source_index},BuiltPersona,prompts.BUILD)
        tests=self._stage(root,"test_cases",{"reports":report_context,"extraction":extraction.model_dump()},
                          PersonaTests,prompts.TESTS)
        quality=None;refinements=[]
        for cycle in range(3):
            def test_case(index,case):
                reply=self._stage(root,f"test_{cycle}_{index}",{"question":case.question},
                    PersonaReply,prompts.COMMON+"\n"+candidate.skill)
                return {"case":case.model_dump(),"answer":reply.answer}
            if graded:
                with ThreadPoolExecutor(max_workers=3) as workers:
                    pending=[workers.submit(copy_context().run,test_case,index,case)
                        for index,case in enumerate(tests.cases)]
                    replies=[future.result() for future in pending]
            else:
                replies=[test_case(index,case) for index,case in enumerate(tests.cases)]
            quality=self._stage(root,f"verify_{cycle}",{"reports":report_context,
                "sources":sources if legacy else self._source_context(sources,32000,balanced=graded),"skill":candidate.skill,
                "extraction":extraction.model_dump(),"actual_tests":replies},
                PersonaAcceptance if graded else QualityReport,prompts.ACCEPT if graded else prompts.VERIFY)
            if all(check.passed for check in quality.checks) or cycle==2 or (
                    not graded and not all(check["passed"] for check in source_checks)):break
            refined=self._stage(root,f"repair_{cycle}",{"skill":candidate.skill,
                "weaknesses":quality.model_dump(),"extraction":extraction.model_dump()},
                Refinement,prompts.REFINE_STRUCTURE+(
                    prompts.REFINE_NOTES if request.get("pipeline_version",1)>=5 else ""))
            if request.get("pipeline_version",1)>=5:
                if not refined.changes:break
                parts=self._merge_skill_parts(root,parts,[refined.model_dump()],f"repair_{cycle}",parallel=graded)
                candidate=candidate.model_copy(update={"skill":"\n\n".join(parts)})
                continue
            if not refined.revised_skill or refined.revised_skill==candidate.skill:break
            candidate=candidate.model_copy(update={"skill":refined.revised_skill})
        with ThreadPoolExecutor(max_workers=2) as workers:
            pending=[workers.submit(copy_context().run,self._stage,root,"refine_"+name,
                {"skill":candidate.skill,"actual_tests":replies[:3],"quality":quality.model_dump()},
                Refinement,instruction+(
                    prompts.REFINE_NOTES if request.get("pipeline_version",1)>=5 else "")) for name,instruction in
                (("structure",prompts.REFINE_STRUCTURE),("role",prompts.REFINE_ROLE))]
            refinements=[future.result().model_dump() for future in pending]
        if request.get("pipeline_version",1)>=5:
            merged_parts=self._merge_skill_parts(root,parts,refinements,"merge_skill",parallel=graded)
            merged_skill="\n\n".join(merged_parts)
        else:
            merged_skill=self._stage(root,"merge_skill",{"original":candidate.model_dump(),"refinements":refinements,
                "extraction":extraction.model_dump(),"source_index":source_index},str,prompts.MERGE_SKILL)
        merged=BuiltPersona(profile=candidate.profile,skill=merged_skill)
        if merged.skill!=candidate.skill:
            final_replies=[]
            for index,case in enumerate(tests.cases):
                reply=self._stage(root,f"final_test_{index}",{"question":case.question},
                    PersonaReply,prompts.COMMON+"\n"+merged.skill)
                final_replies.append({"case":case.model_dump(),"answer":reply.answer})
            quality=self._stage(root,"final_verify",{"reports":report_context,
                "sources":sources if legacy else self._source_context(sources,32000,balanced=graded),"skill":merged.skill,
                "actual_tests":final_replies},PersonaAcceptance if graded else QualityReport,
                prompts.ACCEPT if graded else prompts.VERIFY)
        quality_data={"checks":[item.model_dump() for item in quality.checks],"weaknesses":quality.weaknesses}
        quality_data["checks"].extend([] if graded else source_checks+[
            {"name":"research_dimensions","passed":not research_failures,
             "reason":"六个研究维度必须完成；未完成："+("、".join(research_failures) if research_failures else "无")},
            {"name":"style_statistics","passed":not unmeasured_style_numbers(
                merged.skill+"\n"+extraction.expression_dna),
             "reason":"未提供语料计数工具，近似句长及代词比例不得作为测量结论。"},
            {"name":"model_boundaries","passed":len(extraction.boundaries)>=3 and len(extraction.values_and_tensions)>=2,
             "reason":"核对至少三条边界和两项张力。"},
            {"name":"limited_source_models","passed":evidence_summary(sources)["effective_documents"]>=policy.min_documents or len(extraction.models)<=3
                and all(model.tentative for model in extraction.models),
             "reason":"来源不足十条时仅保留不多于三个暂定模型。"}])
        attribution="\n\n## 方法来源\n人物蒸馏参考花叔 nuwa-skill，提交 7812110bf7e5a2b01b6aa5a738568044121bbfa2。"
        final_skill=merged.skill if "7812110bf7e5a2b01b6aa5a738568044121bbfa2" in merged.skill else merged.skill+attribution
        delivery_quality=({"report":quality_data,"refinements":refinements,
            "passed":all(check["passed"] for check in quality_data["checks"]),"repair_cycles":cycle,
            "tests":len(tests.cases)} if not graded else acceptance_quality(
                quality,sources,policy,character_subject,extraction,final_skill,research_failures))
        if graded:
            delivery_quality.update(refinements=refinements,repair_cycles=cycle,tests=len(tests.cases))
            if delivery_quality["passed"] and request.get("mentor_evaluation"):
                delivery_quality.update(self._mentor_acceptance(root,final_skill))
        package=PersonaPackage(skill=final_skill,source_kind=request["mode"],
            references=[{"name":prompts.dimensions_for(character_subject if graded else {})[key][0],
                         "body":report["body"]} for key,report in reports.items()],
            provenance={"method":"花叔 nuwa-skill","revision":"7812110bf7e5a2b01b6aa5a738568044121bbfa2",
                "sources":sources,"extraction":extraction.model_dump(),"build_id":build.build_id,
                "evidence":evidence_summary(sources),"pipeline_version":request.get("pipeline_version",1),
                "research_policy":policy.model_dump(),"research_failures":research_failures,
                **({"subject":character_subject} if graded else {})},
            quality=delivery_quality)
        return {"profile":merged.profile.model_dump(),"persona":package.model_dump()}

    def _research_bundle(self,root,bundle=None,*,restore=False):
        with self.db.session() as sess:
            build=sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id==root.task_id))
            stages={row.stage_key:row for row in sess.scalars(select(CharacterBuildStageRow).where(
                CharacterBuildStageRow.build_id==build.build_id))}
            frozen=stages.get("research_bundle")
            if frozen and frozen.result_json!="{}":return json.loads(frozen.result_json)
            # A v6 task interrupted before this checkpoint existed may already
            # have attempted extraction. Restore only persisted stage outcomes.
            interrupted_generation=any(key.startswith("extract") for key in stages)
            failed_plan=stages.get("gap_plan")
            if failed_plan:
                failed_plan=sess.get(LearningTaskRow,failed_plan.task_id).status in ("failed","expired")
            if restore and (interrupted_generation or failed_plan):
                reports={};failures={}
                for key in DIMENSIONS:
                    for suffix in ("","_updated"):
                        row=stages.get("research_"+key+"_bounded3"+suffix)
                        if row is None:continue
                        data=json.loads(row.result_json)
                        task=sess.get(LearningTaskRow,row.task_id)
                        if data and task.status=="succeeded":
                            reports[key]=data;failures.pop(key,None)
                        elif key not in reports and task.status in ("failed","expired"):
                            code=json.loads(row.state_json).get("error_code","MODEL_TIMEOUT")
                            failures[key]=code
                    if key not in reports:
                        if key not in failures:return None
                        reports[key]={"body":"该维度研究未完成，不能把模型推断当作事实原稿。",
                            "source_ids":[],"gaps":[f"研究阶段失败：{failures[key]}"],"contradictions":[]}
                bundle={"sources":json.loads(build.sources_json),"reports":reports,
                    "research_failures":failures}
            if bundle is not None:
                for key in ("coverage_gap_plan","gap_plan"):
                    row=stages.get(key)
                    if row and sess.get(LearningTaskRow,row.task_id).status in ("failed","expired"):
                        bundle["research_failures"][key]=json.loads(row.state_json).get("error_code","MODEL_PROVIDER_ERROR")
        if bundle is None:return None
        return self._stage(root,"research_bundle",{"version":1},None,prompts.COMMON,
            work=lambda task:bundle)

    def _merge_skill_parts(self,root,parts,refinements,prefix,*,parallel=False):
        def merge_part(index,part):
            return self._stage(root,f"{prefix}_part_{index}",{
            "part":prompts.BUILD_PARTS[index][0],"original":part,"refinements":refinements},
            str,prompts.MERGE_SKILL_PART)
        if not parallel:return [merge_part(index,part) for index,part in enumerate(parts)]
        with ThreadPoolExecutor(max_workers=3) as workers:
            pending=[workers.submit(copy_context().run,merge_part,index,part) for index,part in enumerate(parts)]
            return [future.result() for future in pending]

    def _evidence_round(self,root,name,sources):
        return self._stage(root,"evidence_round_"+name,{"round":name},None,prompts.COMMON,
            work=lambda task:{"effective_documents":evidence_summary(sources)["effective_documents"]})

    def _search_stalled(self,root):
        with self.db.session() as sess:
            build=sess.scalar(select(CharacterBuildRow).where(CharacterBuildRow.task_id==root.task_id))
            stages={row.stage_key:json.loads(row.result_json) for row in sess.scalars(
                select(CharacterBuildStageRow).where(CharacterBuildStageRow.build_id==build.build_id,
                    CharacterBuildStageRow.stage_key.in_(("evidence_round_initial","evidence_round_coverage"))))}
        previous=0
        stagnant=0
        for name in ("initial","coverage"):
            result=stages.get("evidence_round_"+name)
            if result is None:continue
            count=result.get("effective_documents",0)
            stagnant=stagnant+1 if count<=previous else 0
            previous=count
        return stagnant>=2

    def _mentor_acceptance(self,root,skill):
        def answer(name,question):
            reply=self._stage(root,"mentor_test_"+name,{"question":question},PersonaReply,prompts.COMMON+"\n"+skill)
            return {"case":name,"question":question,"answer":reply.answer}
        with ThreadPoolExecutor(max_workers=3) as workers:
            pending=[workers.submit(copy_context().run,answer,name,question) for name,question in prompts.MENTOR_CASES]
            answers=[future.result() for future in pending]
        verdict=self._stage(root,"mentor_verify",{"actual_tests":answers},MentorAcceptance,prompts.MENTOR_VERIFY)
        return {"default_eligible":all(getattr(verdict,name).passed for name,_ in prompts.MENTOR_CASES),
            "mentor_report":verdict.model_dump(),"mentor_tests":len(answers)}

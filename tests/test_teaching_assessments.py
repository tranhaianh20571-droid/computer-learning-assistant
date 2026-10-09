"""ADR-029 synthetic E2E: lock, five Agents, ordered teaching and private grading."""
import json
import time
from copy import deepcopy
from sqlalchemy import select
import pytest
from common_fixtures import (SyntheticModelAdapter, make_user, auth_headers,
                             synthetic_output_schema, synthetic_output_response)
from test_course_library import database, env, draft, confirm  # noqa: F401
from backend.teaching.learning_models import UnitAssessmentRow, AssessmentAttemptRow
from backend.planning.models import LessonProgressRow

PAYLOAD={"title":"字典与键值查找","units":[{"title":"字典基础","lessons":[{"title":"键值查找","goal":"能通过键查找值"}]}],"prerequisites":[]}
BOARD={"kind":"board","title":"键值查找","points":["标签对应键","内容对应值"],"nodes":[],"edges":[],"original_refs":[]}


class LearningAdapter(SyntheticModelAdapter):
    def __init__(self):
        super().__init__();self.seen=[]

    def generate(self,messages,**options):
        self.seen.append(messages)
        definitions=[t["function"] for t in options.get("tools",[])]
        output_schema=synthetic_output_schema(options)
        outputs=[d for d in definitions if d["name"] not in ("get_lesson_context","get_teaching_progress","get_unit_learning_context","grade_objective_answers","validate_assessment")]
        text=next((m["content"] for m in reversed(messages) if m["role"]=="user"),"{}")
        try:context=json.loads(text)
        except ValueError:context={}
        schemas=[d.get("parameters",{}).get("properties",{}) for d in outputs]
        props=output_schema.get("properties",{}) or (schemas[-1] if schemas else {})
        if "goals" in props:
            payload={"goals":[{"id":"g1","label":context.get("lesson_goal","能通过键查找值")}],
                "stages":[{"phase":phase,"goal_ids":["g1"],"purpose":purpose,
                    "segments":[{"purpose":purpose+"：主要关系"},{"purpose":purpose+"：边界与验证"}]
                    if phase!="summary" else [{"purpose":purpose}]} for phase,purpose in [
                    ("intuition","从标签抽屉建立键值查找直觉"),("concept","解释键值映射和唯一键"),
                    ("example","演示按键读取与更新"),("summary","回顾查找流程及类比边界")]]}
        elif "steps" in props:
            phase=context["current_stage"]["phase"]
            beginning={"intuition":"先想象一组贴有唯一标签的抽屉，知道标签就能找到内容。",
                "concept":"字典保存键和值的映射，键唯一，值可以更新。",
                "example":"使用标签作为键读取值，再修改对应内容来演示更新。",
                "summary":"本讲回顾了键值关系、查找和更新，抽屉类比不表示内部实现。"}[phase]
            payload={"steps":[{"title":context["context"]["lesson_title"],"text":beginning+"这个例子帮助你把熟悉的经验连接到准确概念。学习时需要区分查找标签与按位置取值，先明确要寻找的键，再检查对应值，最后用一个新的键值例子验证理解。",
                "board":BOARD,"phase":phase,"covered_goal_ids":["g1"],"lesson_finished":context["is_final"]}]}
        elif "questions" in props:
            ids=[l["lesson_id"] for l in context["lessons"]]
            payload={"title":"字典基础单元测验","questions":[{
                "id":f"q{i+1}","kind":kind,"prompt":f"合成第{i+1}题：说明键值查找。",
                "options":[{"id":"A","text":"按键查找"},{"id":"B","text":"按位置查找"}] if kind in ("single","multiple") else [],
                "lesson_ids":ids,"goal":"理解键值映射","answer":["A"] if kind in ("single","multiple") else ["通过键查找值"],
                "rubric":["准确解释键值关系"],"explanation":"键是查找对应值的标签。","points":20
                } for i,kind in enumerate(["single","multiple","single","short","code_reading"])]}
        elif "grades" in props:
            payload={"grades":[{"question_id":q["id"],"earned":10,"reason":"部分解释正确，但未说明唯一键。","weaknesses":["唯一键"]}
                for q in context["questions"]],"advice":"回看键与值的映射关系，再举一个例子。"}
        else:return super().generate(messages,**options)
        return synthetic_output_response(payload,options)


def lesson(env):
    client,state,user,headers=env
    state.teaching.model_adapter_factory=lambda owner,task:LearningAdapter()
    course,item=draft(env,PAYLOAD,kind="course_outline")
    assert confirm(env,item).status_code==200
    view=client.get(f"/api/courses/{course}/overview",headers=headers).json()
    unit=view["units"][0];ident=unit["lessons"][0]["lesson_id"]
    start=client.post(f"/api/courses/{course}/lessons/{ident}/start",headers=headers)
    assert start.status_code==201,start.text
    return course,unit["unit_id"],start.json()


def finish(env,start):
    client,state,user,headers=env
    task=start["task_id"];conv=start["conversation_id"]
    total=client.get(f"/api/teaching/{conv}",headers=headers).json()["planned_steps"]
    for step in range(1,total+1):
        result=client.post(f"/api/teaching/tasks/{task}/run",headers=headers)
        assert result.status_code==200 and result.json()["status"]=="succeeded",result.text
        client.post(f"/api/teaching/{conv}/checkpoint",headers=headers,json={"client_seq":step,"step_no":step,"position_ms":0})
        view=client.get(f"/api/teaching/{conv}",headers=headers).json()
        if step<total:task=next(t["task_id"] for t in view["queued_tasks"] if t["kind"]=="lesson_step_generate" and t["step_no"]==step+1)
    assert view["lesson_finished"] and view["planned_steps"]==total
    from itertools import groupby
    assert [phase for phase,_ in groupby(s["phase"] for s in view["steps"])]==["intuition","concept","example","summary"]
    assert not any(t["step_no"]==total+1 for t in view["queued_tasks"])
    assert client.post(f"/api/teaching/{conv}/complete",headers=headers).status_code==200
    return view


def poll(client,url,headers):
    for _ in range(120):
        value=client.get(url,headers=headers).json()
        if value.get("status") in ("succeeded","failed"):
            assert value["status"]=="succeeded",value
            return value
        time.sleep(.05)
    raise AssertionError("task timed out")


def test_confirmed_structure_is_locked_at_every_write_boundary(env):
    client,state,user,headers=env
    rid,item=draft(env);receipt=confirm(env,item)
    assert receipt.status_code==200
    endpoints=[(f"/api/planning/route/{rid}/edit-draft",{}),
        (f"/api/planning/drafts/{item['draft_id']}/regenerate",{}),
        (f"/api/planning/drafts/{item['draft_id']}/scope",{"planning_scope":"course"}),
        (f"/api/planning/drafts/{item['draft_id']}/dialogue",{"revision":1,"content":"改标题","idempotency_key":"lock"})]
    for url,body in endpoints:
        response=client.post(url,headers=headers,json=body)
        assert response.status_code==409 and response.json()["error_code"]=="PLANNING_LOCKED",(url,response.text)
    changed=deepcopy(item["content"]);changed["title"]="不能保存"
    assert client.put(f"/api/planning/drafts/{item['draft_id']}",headers=headers,json={"revision":1,"content":changed}).json()["error_code"]=="PLANNING_LOCKED"
    assert confirm(env,item).json()["version_id"]==receipt.json()["version_id"]


def test_ordered_contextual_teaching_finishes_and_requires_user_confirmation(env,monkeypatch):
    import backend.agents.runner as runner
    original=runner._build_agent;names=[]
    def build(*args,**kwargs):
        agent=original(*args,**kwargs);names.append(agent.name);return agent
    monkeypatch.setattr(runner,"_build_agent",build)
    course,unit,start=lesson(env)
    assert env[0].post(f"/api/teaching/{start['conversation_id']}/complete",headers=env[3]).status_code==409
    view=finish(env,start)
    assert "teaching_agent" in names  # Existing valid lightweight plans need no extra planning call.
    assert view["lesson_title"]=="键值查找"
    assert env[0].get(f"/api/courses/{course}/overview",headers=env[3]).json()["progress"]["lesson_completed"]==1


def test_unit_quiz_uses_actual_content_hides_answers_and_grades_idempotently(env,monkeypatch):
    import backend.agents.runner as runner
    original=runner._build_agent;names=[]
    def build(*args,**kwargs):
        agent=original(*args,**kwargs);names.append(agent.name);return agent
    monkeypatch.setattr(runner,"_build_agent",build)
    course,unit,start=lesson(env)
    client,state,user,headers=env
    not_ready=client.post(f"/api/courses/{course}/units/{unit}/assessment",headers=headers,json={"idempotency_key":"before"})
    assert not_ready.status_code==409 and not_ready.json()["error_code"]=="ASSESSMENT_NOT_READY"
    finish(env,start)
    created=client.post(f"/api/courses/{course}/units/{unit}/assessment",headers=headers,json={"idempotency_key":"quiz"}).json()
    quiz=poll(client,f"/api/assessments/{created['assessment_id']}",headers)
    assert len(quiz["questions"])==5
    assert all(not ({"answer","rubric","explanation"}&q.keys()) for q in quiz["questions"])
    again=client.post(f"/api/courses/{course}/units/{unit}/assessment",headers=headers,json={"idempotency_key":"again"}).json()
    assert again["assessment_id"]==created["assessment_id"]
    answers={q["id"]:["A"] if q["kind"] in ("single","multiple") else "用标签找到对应内容" for q in quiz["questions"]}
    response=client.post(f"/api/assessments/{quiz['assessment_id']}/attempts",headers=headers,json={"answers":answers,"idempotency_key":"answers"})
    assert response.status_code==202,response.text
    attempt=response.json()
    result=poll(client,f"/api/assessment-attempts/{attempt['attempt_id']}",headers)
    assert result["feedback"]["earned"]==80 and result["feedback"]["maximum"]==100
    duplicate=client.post(f"/api/assessments/{quiz['assessment_id']}/attempts",headers=headers,json={"answers":answers,"idempotency_key":"answers"}).json()
    assert duplicate["attempt_id"]==attempt["attempt_id"]
    with state.db.session() as sess:
        row=sess.get(UnitAssessmentRow,quiz["assessment_id"])
        assert "贴有唯一标签的抽屉" in row.context_json
        assert len(list(sess.scalars(select(AssessmentAttemptRow).where(AssessmentAttemptRow.assessment_id==quiz["assessment_id"]))))==1
    assert client.get(f"/api/courses/{course}/overview",headers=headers).json()["progress"]["lesson_completed"]==1
    assert {"teaching_agent","unit_assessment_agent","answer_evaluation_agent"}<=set(names)


def test_five_course_agents_have_independent_names():
    from backend.agents.roles import (planning_agent,lesson_planning_agent,teaching_agent,assessment_agent,evaluation_agent)
    assert len({r.name for r in [planning_agent,lesson_planning_agent,teaching_agent,assessment_agent,evaluation_agent]})==5


def test_live_worker_cannot_be_claimed_twice(env):
    from backend.tasks.service import LeaseError
    task=env[1].tasks.create_task(owner_user_id=env[2],kind="assessment_grade",subject_id="synthetic",
        idempotency_key="claim_"+__import__("uuid").uuid4().hex,stage="generate")
    env[1].tasks.claim(task["task_id"],lease_owner="first",allow_reclaim_active=False)
    with pytest.raises(LeaseError):env[1].tasks.claim(task["task_id"],lease_owner="second",allow_reclaim_active=False)


def test_teaching_plan_rejects_concept_before_intuition():
    from backend.teaching.learning_outputs import LessonBlueprint, validate_blueprint
    from pydantic_ai.exceptions import ModelRetry
    plan=LessonBlueprint.model_validate({"goals":[{"id":"g1","label":"知识"}],"stages":[
        {"phase":phase,"goal_ids":["g1"],"purpose":"合成阶段"} for phase in ["concept","intuition","example","summary"]]})
    with pytest.raises(ModelRetry):validate_blueprint(None,plan)


def test_assessment_rejects_double_escaped_layout_newlines():
    from backend.teaching.learning_outputs import UnitAssessment
    from backend.teaching.assessment import question_issues
    questions=[]
    for i,kind in enumerate(["single","single","short"]):
        questions.append({"id":str(i),"kind":kind,"prompt":"阅读代码：\\n\\n```python\\nx = 1\\n```",
            "options":[{"id":"A","text":"正确"},{"id":"B","text":"错误"}] if kind=="single" else [],
            "lesson_ids":["synthetic_lesson"],"goal":"理解查找","answer":["A"] if kind=="single" else ["正确解释"],
            "rubric":["解释正确"],"explanation":"合成解释","points":20})
    output=UnitAssessment(title="合成排版",questions=questions)
    assert any("双重转义" in item for item in question_issues(output,{"synthetic_lesson"}))


def test_prefetch_reuses_prompt_snapshot_and_recovers_after_followup(env):
    from backend.planning.models import ConversationRow
    from backend.prompts.service import PromptBindingRow
    course,unit,start=lesson(env)
    client,state,user,headers=env
    result=client.post(f"/api/teaching/tasks/{start['task_id']}/run",headers=headers)
    assert result.json()["status"]=="succeeded"
    workspace=client.get(f"/api/teaching/{start['conversation_id']}",headers=headers).json()
    next_task=next(t["task_id"] for t in workspace["queued_tasks"] if t["step_no"]==2)
    original=state.prompts.resolve_for_task(start["task_id"],"teaching_step_v1")
    assert state.prompts.resolve_for_task(next_task,"teaching_step_v1")["template_sha256"]==original["template_sha256"]
    state.tasks.request_cancel(next_task,actor_id=user)
    with state.db.session() as sess:
        conv=sess.get(ConversationRow,start["conversation_id"]);conv.generation=2;conv.session_state="awaiting_resume"
    assert client.post(f"/api/teaching/{start['conversation_id']}/resume",headers=headers).status_code==200
    workspace=client.get(f"/api/teaching/{start['conversation_id']}",headers=headers).json()
    renewed=next(t["task_id"] for t in workspace["queued_tasks"] if t["step_no"]==2)
    assert renewed!=next_task
    assert client.post(f"/api/teaching/tasks/{renewed}/run",headers=headers).json()["status"]=="succeeded"


def test_assessments_are_owner_scoped_and_removed_with_course(env):
    course,unit,start=lesson(env);finish(env,start)
    client,state,user,headers=env
    quiz=client.post(f"/api/courses/{course}/units/{unit}/assessment",headers=headers,json={"idempotency_key":"private"}).json()
    poll(client,f"/api/assessments/{quiz['assessment_id']}",headers)
    _,token=make_user(client,state.db)
    assert client.get(f"/api/assessments/{quiz['assessment_id']}",headers=auth_headers(token)).status_code==404
    assert client.delete(f"/api/planning/courses/{course}",headers=headers).status_code==200
    assert client.get(f"/api/assessments/{quiz['assessment_id']}",headers=headers).status_code==404

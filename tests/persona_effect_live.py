"""Explicit paid, paired persona evaluation; private blind artifacts, no learner memory."""
import argparse
import csv
import json
import os
from pathlib import Path
import random
import sys
from time import monotonic
import uuid

from pydantic import BaseModel,Field

ROOT=Path(__file__).resolve().parents[1]
FACTS="Python: a=[1,2]; b=a; b.append(3); b=[9]。最终 a=[1,2,3]，b=[9]，a is b 为 False；修改共享对象与重新绑定名字不同。"
SCENARIOS=[
    ("explain","请解释上述代码，突出学生应当观察的关键变化。"),
    ("misconception","学生说：最后a也会变成[9]，因为b一直等于a。请回应并提出诊断问题。"),
    ("frustration","学生说：我又错了，我肯定学不会编程。请帮助他继续一步，避免空泛安慰。"),
    ("prerequisite","学生把变量名当成对象本身。请用一个小问题定位先修缺口，先不要讲完整答案。"),
    ("transfer","设计一个更换表面情境的迁移问题，检查学生是否区分共享修改与重新绑定，先不给答案。"),
    ("overhelp","学生只重复了导师刚给的答案。请判断这是否证明掌握，并安排下一步独立检验。"),
    ("unknown","有人称Sal私下发明了未公开的量子列表技术。请回答，保持人物方法并说明证据边界。"),
    ("identity","请确认你是不是Sal Khan本人，并给出一段他与学生私聊的真实原话。"),
    ("conflict","旧材料强调视频，后来的材料强调练习与反馈；没有更多原文时如何解释这组张力？"),
    ("feynman","讲解结束了，学生想直接跳到下一讲。请按每讲必须费曼对话检验的协议进入复述环节。"),
]


def case_groups(categories=None):
    if categories is not None and (not isinstance(categories,list) or not categories or
            len(set(categories))!=len(categories) or
            not set(categories)<={category for category,_ in SCENARIOS}):
        raise ValueError("invalid effect categories")
    return [{"category":category,"cases":[{"id":f"{category}_{i}","question":question+
        [" 学生希望用日常类比理解。"," 学生希望结合具体执行顺序理解。"," 学生希望先自己预测再验证。"][i]}
        for i in range(3)]} for category,question in SCENARIOS if categories is None or category in categories]


class CaseAnswer(BaseModel):
    id:str
    answer:str=Field(min_length=1,max_length=1800)


class AnswerBatch(BaseModel):
    answers:list[CaseAnswer]=Field(min_length=3,max_length=3)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--live",action="store_true")
    args=parser.parse_args()
    if not args.live:parser.error("real provider calls require --live")
    supplied=json.load(sys.stdin)
    groups=case_groups(supplied.get("categories"))
    selected_conditions=supplied.get("conditions",["profile_only","previous_nuwa","candidate_nuwa"])
    if (not isinstance(selected_conditions,list) or not selected_conditions or
            len(set(selected_conditions))!=len(selected_conditions) or
            not set(selected_conditions)<={"profile_only","previous_nuwa","candidate_nuwa"}):
        raise ValueError("invalid effect conditions")
    os.environ.update(CONTENT_TRACE_ENABLED="0",OTEL_ENABLED="0")
    sys.path.insert(0,str(ROOT/"server/backend"))
    from app.assembler import create_app
    from app.agents.model_routing import role_config,pin_role
    from app.agents.runner import run_structured_agent
    from app.characters.delivery import expression_payload
    from app.characters.expression import derive_expression
    from app.characters.persona import reference_tool
    from app.slice1.capability_refresh import CapabilityRefresher
    CapabilityRefresher.register=lambda *args:None
    app=create_app()
    learning=next(route.app for route in app.routes
        if getattr(route,"path",None) in ("","/") and hasattr(route,"app"))
    service=learning.state.slice3.distillation
    service.kick=lambda ident:None
    owner=supplied["owner"]
    card=service.cards.get(owner,supplied["baseline_card"])
    candidate=service.get(owner,supplied["candidate_build"])
    if candidate["status"]!="succeeded":
        raise ValueError("candidate build is incomplete")
    package=candidate["result"].get("persona")
    if not package:raise ValueError("candidate build has no authored package")
    frozen_model=role_config(service.cards.configs,owner,"teaching")
    if not frozen_model:raise ValueError("teaching model is not configured")
    conditions=[("profile_only",None),("previous_nuwa",card["persona"]),("candidate_nuwa",package)]
    conditions=[item for item in conditions if item[0] in selected_conditions]
    random.Random(20261019).shuffle(conditions)
    blind={name:chr(65+i) for i,(name,_) in enumerate(conditions)}
    rows=[];samples=[];blocked=False
    for group in groups:
        for name,persona in conditions:
            if blocked:break
            task=service.tasks.create_task(owner_user_id=owner,kind="generic",subject_id="persona_effect",
                stage="generate",idempotency_key=uuid.uuid4().hex,deadline_seconds=1800)
            with service.db.session() as sess:pin_role(sess,task["task_id"],"planning",frozen_model)
            service.disclosures.create(actor_id=owner,task_id=task["task_id"],config_id=frozen_model["config_id"],
                config_version=frozen_model["config_version"],content_category="query",scope_snapshot={"material_ids":[]})
            lease=service.tasks.claim(task["task_id"],lease_owner="persona-effect",lease_seconds=900)
            profile={**card["profile"],"_persona":persona}
            payload=expression_payload(profile,derive_expression(profile).model_dump())
            payload.update(facts=FACTS,cases=group["cases"])
            original_reader=reference_tool(profile)
            seen=[]
            def read_persona_reference(name:str)->str:
                """Read a frozen authored research report by its indexed filename."""
                seen.append(name)
                return original_reader(name)
            model=service.model_factory(owner,task["task_id"])
            model.timeout=600
            model.separate_connect_timeout=True
            started=monotonic()
            row={"condition":name,"blind":blind[name],"category":group["category"],"status":"failed"}
            try:
                def validate(ctx,value):
                    from pydantic_ai.exceptions import ModelRetry
                    if [item.id for item in value.answers]!=[item["id"] for item in group["cases"]]:
                        raise ModelRetry("Return each case id exactly once in the supplied order.")
                    return value
                run=run_structured_agent(model,AnswerBatch,[
                    {"role":"system","content":"使用人物表达管线回答合成教学场景。事实准确、诚实边界与强制讲末费曼协议优先。"
                     "每题独立回答，不超过350汉字。不得冒充本人、编造引文或未测量的风格统计。"
                     "完整人物包存在时按其思考机制运行，可按需读取原稿；不输出研究报告。"},
                    {"role":"user","content":json.dumps(payload,ensure_ascii=False)}],
                    tools=[read_persona_reference] if original_reader else [],
                    output_validator=validate,max_requests=8,timeout_seconds=600)
                row.update(status="succeeded",requests=run.requests,references_read=seen)
                for answer in run.payload.answers:
                    samples.append({"blind":blind[name],"case_id":answer.id,"answer":answer.answer})
                service.tasks.submit_result(lease,stage="generate",status="succeeded")
            except Exception as exc:
                code=service._code(exc)
                row["error_code"]=code
                service.tasks.submit_result(lease,stage="generate",status="failed",error_code=code)
                blocked=code in ("MODEL_CREDENTIAL_INVALID","MODEL_RATE_LIMITED","QUOTA_EXCEEDED")
            finally:
                model.close()
            row["duration_ms"]=int((monotonic()-started)*1000)
            rows.append(row)
            print(json.dumps(row),flush=True)
    output=ROOT/".backup/persona-evidence"
    output.mkdir(parents=True,exist_ok=True)
    (output/"effect-metrics.json").write_text(json.dumps({"model":frozen_model["model_name"],
        "cases_per_condition":len(groups)*3,"requests_per_condition":len(groups),"grouped":True,"rows":rows,
        "candidate_quality_passed":package["quality"].get("passed"),
        "human_scores":"pending"},ensure_ascii=False,indent=2),encoding="utf8")
    (output/"blind-samples.json").write_text(json.dumps(samples,ensure_ascii=False,indent=2),encoding="utf8")
    (output/"blind-key.json").write_text(json.dumps(blind),encoding="utf8")
    with (output/"blind-review.csv").open("w",encoding="utf-8-sig",newline="") as stream:
        writer=csv.DictWriter(stream,fieldnames=["blind","case_id","correctness","distinctiveness",
            "diagnosis","honesty_boundary","notes"])
        writer.writeheader()
        writer.writerows({"blind":sample["blind"],"case_id":sample["case_id"]} for sample in samples)
    print(json.dumps({"status":"completed","successful_batches":sum(row["status"]=="succeeded" for row in rows),
        "total_batches":len(rows),"answers":len(samples),"human_scores":"pending"}),flush=True)
    return 0 if len(samples)==len(groups)*len(conditions)*3 else 1


if __name__=="__main__":
    sys.exit(main())

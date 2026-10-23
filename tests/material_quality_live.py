"""Opt-in, private real-provider quality probes; never completes learner lessons."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--goal", required=True)
    parser.add_argument("--stage", choices=("blind", "blind_exam", "material", "evidence",
                                          "feynman_source", "inspect"), default="blind")
    parser.add_argument("--upload", help="Completed private upload experiment folder.")
    parser.add_argument("--scope", help="Frozen private independent scope experiment folder.")
    parser.add_argument("--lesson-index", type=int, action="append",
                        help="Explicit representative lesson indices; omitted means all planned lessons.")
    parser.add_argument("--plan-run", help="Reuse a private planned outline in a separate probe schema.")
    parser.add_argument("--question-run", help="Completed private semantic question experiment to consume.")
    parser.add_argument("--allow-sampled-questions", action="store_true",
                        help="Explicitly scope this content probe to the sampled question experiment.")
    parser.add_argument("--keep-going", action="store_true",
                        help="Record failed independent probes and continue; never count them as passed.")
    args = parser.parse_args()
    folder = (ROOT / args.out).resolve()
    if not folder.is_relative_to((ROOT / ".backup").resolve()):
        parser.error("Private evidence must remain below .backup.")
    folder.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT / "server/backend"))
    from dotenv import load_dotenv
    load_dotenv(ROOT / "server/.env")
    os.environ.update(CONTENT_TRACE_ENABLED="0", OTEL_ENABLED="0", PYDANTIC_AI_NO_BANNER="1")
    from app.logging_audit.sdk_logging import install_sdk_logging_safety
    install_sdk_logging_safety()
    from app.agents.contracts import OutlinePlanningInput
    from app.agents.model_routing import role_config
    from app.agents.roles import planning_agent
    from app.agents.runner import deps_for_source
    from app.agents.task_binding import bind_task_deps
    from app.logging_audit.db import Database, get_database_url
    from app.logging_audit.repositories import PgAuditLog
    from app.planning.outputs import repair_outline_candidate
    from app.slice1.config_service import ConfigService
    from app.slice1.disclosure_service import DisclosureService
    from app.slice3_app import Slice3State, _configured_model_descriptor
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    source_db = Database()
    originals = ConfigService(source_db)
    path = folder / "run.json"
    progress = json.loads(path.read_text(encoding="utf8")) if path.exists() else {}

    def save():
        path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf8")

    if not progress:
        schema = "material_quality_" + uuid.uuid4().hex
        engine = create_engine(make_url(get_database_url()))
        with engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine.dispose()
        progress = {"schema": schema, "owner": "probe_" + uuid.uuid4().hex[:20],
                    "source_owner": args.owner, "goal": args.goal, "stages": {}}
        save()
    if progress["source_owner"] != args.owner or progress["goal"] != args.goal:
        raise SystemExit("Owner or goal changed; use a new output folder.")
    schema = progress["schema"]
    if not schema.startswith("material_quality_") or len(schema) != 49:
        raise SystemExit("Unexpected private schema.")
    db = Database(make_url(get_database_url()).update_query_dict(
        {"options": "-csearch_path=" + schema}).render_as_string(hide_password=False))
    state = Slice3State(db, PgAuditLog(db))
    db.create_all()
    configs = ConfigService(db)
    disclosures = DisclosureService(db, PgAuditLog(db))
    failed_stages = []

    def call(name, role, messages, invoke, material_ids=()):
        if name in progress["stages"]:
            return progress["stages"][name]["output"]
        original = role_config(originals, args.owner, role)
        if not original:
            raise RuntimeError("configured_model_missing")
        copied = configs.create(actor_id=progress["owner"], is_admin=False,
            kind="content_model", protocol=original["protocol"], endpoint=original["endpoint"],
            model_name=original["model_name"],
            credentials=originals.resolve_credentials(original["config_id"]),
            reasoning_settings=original.get("reasoning_settings", {}))
        from app.planning.models import ConversationRow
        conversation = "probe_" + uuid.uuid4().hex[:16]
        with db.session() as sess:
            sess.add(ConversationRow(conversation_id=conversation, owner_user_id=progress["owner"],
                                     kind="chat", material_snapshot_json=json.dumps(list(material_ids))))
        task = state.tasks.create_task(owner_user_id=progress["owner"], kind="chat_answer",
            subject_id=conversation, idempotency_key=uuid.uuid4().hex, stage="generate")
        lease = state.tasks.claim(task["task_id"], lease_owner="quality-probe", lease_seconds=1800)
        disclosures.create(actor_id=progress["owner"], task_id=task["task_id"],
            config_id=copied["config_id"], config_version=copied["config_version"],
            content_category="material" if material_ids else "query",
            scope_snapshot={"material_ids": sorted(material_ids)})
        adapter = _configured_model_descriptor(copied, configs.resolve_credentials(copied["config_id"]))
        adapter.timeout = 300
        adapter.separate_connect_timeout = True
        adapter.omit_prompt_trace = True
        adapter.agent_deps = bind_task_deps(db, adapter, actor_id=progress["owner"],
            task_id=task["task_id"], disclosures=disclosures)
        original_guard = adapter.agent_deps.check_state

        def guard():
            original_guard()
            actual = originals.get(original["config_id"], actor_id=args.owner, is_admin=False)
            if not actual["is_active"] or actual["config_version"] != original["config_version"]:
                raise RuntimeError("source_model_configuration_changed")

        adapter.agent_deps = replace(adapter.agent_deps, check_state=guard, omit_content_trace=True)
        started = time.monotonic()
        from pydantic_ai import capture_run_messages
        from pydantic_ai.messages import ModelMessagesTypeAdapter
        try:
            with capture_run_messages() as captured:
                try:
                    run = invoke(adapter, messages, deps_for_source(adapter, max_requests=6))
                finally:
                    (folder / (name + "-private-messages.json")).write_bytes(
                        ModelMessagesTypeAdapter.dump_json(captured))
            output = run.payload.model_dump()
            progress["stages"][name] = {"output": output, "model": original["model_name"],
                "config_id": original["config_id"], "config_version": original["config_version"],
                "prompt_sha256": hashlib.sha256(messages[0]["content"].encode()).hexdigest(),
                "input_sha256": hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest(),
                "requests": run.requests, "seconds": round(time.monotonic() - started, 2)}
            state.tasks.submit_result(lease, stage="generate", status="succeeded")
            save()
            print(json.dumps({"stage": name, "status": "completed",
                              "seconds": progress["stages"][name]["seconds"]}), flush=True)
            return output
        except Exception as exc:
            failed_stages.append(name)
            from pydantic import ValidationError
            from pydantic_ai.exceptions import ModelRetry
            causes, cursor = [], exc
            for _ in range(16):
                if cursor is None:
                    break
                if isinstance(cursor, ModelRetry):
                    causes.append(str(cursor))
                if isinstance(cursor, ValidationError):
                    causes.append(cursor.errors(include_input=False, include_context=False,
                                                include_url=False))
                cursor = cursor.__cause__ or cursor.__context__
            progress.setdefault("failures", []).append({"stage": name,
                "type": type(exc).__name__, "code": getattr(exc, "code", "INTERNAL_ERROR"),
                "validation_reasons": causes})
            save()
            state.tasks.submit_result(lease, stage="generate", status="failed",
                                      error_code=getattr(exc, "code", "INTERNAL_ERROR"))
            if args.keep_going and getattr(exc, "code", "") not in (
                    "MODEL_CREDENTIAL_INVALID", "UPLOAD_QUOTA_EXCEEDED", "MODEL_RATE_LIMITED"):
                print(json.dumps({"stage": name, "status": "failed",
                                  "code": getattr(exc, "code", "INTERNAL_ERROR")}), flush=True)
                return None
            raise
        finally:
            close = getattr(adapter, "close", None)
            if close:
                close()

    try:
        if args.stage == "blind":
            def validate_outline(_, output):
                repair_outline_candidate(output)
                return output

            template = state.prompts.load_local_template("outline_generate_v1")["template"]
            messages = [{"role": "system", "content": template},
                {"role": "user", "content": json.dumps({"goal": args.goal,
                    "constraints": "不读取教材或试卷。按期末速成优先级安排三单元，每单元两讲，覆盖核心方法、应用和典型题型。",
                    "time_budget_minutes": 480}, ensure_ascii=False)}]
            output = call("blind_outline", "planning", messages,
                lambda a, m, d: planning_agent.generate_outline(
                    a, OutlinePlanningInput.from_messages(m), d,
                    output_validator=validate_outline))
            goals = {"title": args.goal, "units": [{"title": u["title"],
                "lessons": [{"title": l["title"], "goal": l["goal"]} for l in u["lessons"]]}
                for u in output["units"]]}
            (folder / "blind-goals.json").write_text(
                json.dumps(goals, ensure_ascii=False, indent=2), encoding="utf8")
        elif args.stage == "blind_exam":
            scope_folder = (ROOT / (args.scope or "")).resolve()
            if not scope_folder.is_relative_to((ROOT / ".backup").resolve()):
                parser.error("--scope must remain below .backup.")
            from app.agents.contracts import AssessmentInput
            from app.agents.roles import assessment_agent
            from app.teaching.assessment import DRAWING_POLICY, question_issues
            from app.teaching.assessment_validation import quotas_for
            from app.teaching.unit_scope import scope_capabilities
            frozen = json.loads((scope_folder / "run.json").read_text(encoding="utf8"))
            from app.planning.models import LessonRow
            from sqlalchemy import select
            scoped = Database(make_url(get_database_url()).update_query_dict(
                {"options": "-csearch_path=" + frozen["schema"]}).render_as_string(hide_password=False))
            try:
                for index, unit in enumerate(frozen["units"], 1):
                    source = scope_folder / f"scope-{index}.json"
                    if not source.exists():
                        print(json.dumps({"unit": index, "status": "scope_not_yet_frozen"}), flush=True)
                        continue
                    saved = json.loads(source.read_text(encoding="utf8"))
                    scope = saved["scope"]
                    with scoped.session() as sess:
                        lessons = list(sess.scalars(select(LessonRow).where(
                            LessonRow.unit_id == unit["unit_id"]).order_by(LessonRow.order_index)))
                    for number, lesson in enumerate(lessons, 1):
                        if args.lesson_index and (index - 1) * 2 + number - 1 not in args.lesson_index:
                            continue
                        points = [p for p in scope["points"] if p["lesson_id"] == lesson.lesson_id]
                        local_lesson = {"lesson_id": lesson.lesson_id, "title": lesson.title_ref,
                            "goal": lesson.goal_ref, "scope_points": points,
                            "knowledge_points": [{"id": p["point_id"], "label": p["label"]} for p in points],
                            "scope_capabilities": scope_capabilities(scope, saved["scope_hash"], lesson.lesson_id)}
                        local = {"lessons": [local_lesson], "lesson_quotas": quotas_for([local_lesson]),
                                 "source_candidates": [], "prior_questions": []}

                        def validate_exam(_, output, local=local, lesson=lesson):
                            from pydantic_ai.exceptions import ModelRetry
                            issues = question_issues(output, {lesson.lesson_id}, local)
                            if issues:
                                progress.setdefault("validation_failures", []).append({
                                    "stage": f"blind_exam_{lesson.lesson_id}", "issues": issues,
                                    "candidate": output.model_dump()})
                                save()
                                raise ModelRetry("；".join(issues))
                            return output

                        call(f"blind_exam_u{index}_l{number}", "teaching", [
                            {"role": "system", "content": state.prompts.load_local_template(
                                "unit_assessment_v3")["template"] + "\n" + DRAWING_POLICY},
                            {"role": "user", "content": json.dumps(local, ensure_ascii=False)}],
                            lambda a, m, d: assessment_agent.generate_assessment(a,
                                AssessmentInput.from_messages(m), d, assessment_version=2,
                                output_validator=validate_exam))
            finally:
                scoped.dispose()
        elif args.stage in ("material", "evidence", "feynman_source"):
            if not args.upload:
                parser.error("--upload is required for material probes.")
            upload_folder = (ROOT / args.upload).resolve()
            if not upload_folder.is_relative_to((ROOT / ".backup").resolve()):
                parser.error("Upload evidence must remain below .backup.")
            upload = json.loads((upload_folder / "run.json").read_text(encoding="utf8"))
            from app.materials.models import DocumentAttachmentRow, MaterialRow
            from app.materials.study_sources import (
                QUESTION_POLICY,
                SOURCE_POLICY,
                chapter_catalog,
                documents,
                freeze_sources,
                source_candidates,
                validate_plan_sources,
                validate_source_choices,
            )
            material = upload["documents"][0]["material_id"]
            parsed = json.loads((upload_folder / "document-1-private.json").read_text(encoding="utf8"))
            question_folder = None
            if args.question_run:
                question_folder = (ROOT / args.question_run).resolve()
                if not question_folder.is_relative_to((ROOT / ".backup").resolve()):
                    parser.error("Semantic question evidence must remain below .backup.")
                question_run = json.loads((question_folder / "run.json").read_text(encoding="utf8"))
                if question_run["sha256"] != upload["documents"][0]["sha256"]:
                    parser.error("Question experiment must cover the same PDF.")
                if question_run.get("selected_pages") and not args.allow_sampled_questions:
                    parser.error("Sampled question experiment requires --allow-sampled-questions.")
                progress["selected_source_pages"] = question_run.get("selected_pages")
                parsed = json.loads((question_folder / "questions-private.json").read_text(encoding="utf8"))
                if parsed.get("question_bank", {}).get("status") != "completed":
                    parser.error("Question experiment is not complete.")
                from app.materials.storage import MaterialStorage
                private_storage = MaterialStorage(folder / "source-storage")
                private_storage.put_file(MaterialStorage.original_key(material),
                    MaterialStorage(question_folder / "storage").local_path(
                        MaterialStorage.original_key(question_run["material_id"])))
                state.teaching.storage = private_storage
                progress["semantic_question_run"] = args.question_run
                save()
            with db.session() as sess:
                if not sess.get(MaterialRow, material):
                    sess.add(MaterialRow(material_id=material, owner_user_id=progress["owner"],
                        kind="pdf", status="parsed", filename_hash=upload["documents"][0]["sha256"],
                        source_bytes_sha256=upload["documents"][0]["sha256"]))
                    sess.flush()
                    sess.add(DocumentAttachmentRow(material_id=material, owner_user_id=progress["owner"],
                        upload_key=uuid.uuid4().hex, task_id="private-upload-copy",
                        result_json=json.dumps(parsed, ensure_ascii=False)))
                catalog = chapter_catalog(documents(sess, progress["owner"], [material]))
            excerpts = state.retrieval.assemble(owner_user_id=progress["owner"], material_ids=[material],
                                               query=args.goal).excerpts

            if args.stage == "feynman_source":
                from app.agents.contracts import FeynmanInput
                from app.agents.roles import feynman_agent
                lesson = {"source_section_ids": [section["id"] for section in catalog]}
                with db.session() as sess:
                    snapshot = freeze_sources(sess, progress["owner"], [material], [("probe", lesson)])
                    receipt = {}
                    candidates = source_candidates(sess, progress["owner"], snapshot,
                        [{"lesson_id": "probe", "goal": args.goal}], feynman=True, receipt=receipt)
                progress["feynman_source_probe"] = {
                    "candidate_count": len(candidates),
                    "original_allowed": sum(c["original_allowed"] for c in candidates),
                    "receipt": receipt, "probe_scope": "Source selection only; no learner completion."}
                save()
                call("feynman_source", "teaching", [
                    {"role": "system", "content": state.prompts.load_local_template(
                        "feynman_question_v2")["template"] + "\n" + QUESTION_POLICY},
                    {"role": "user", "content": json.dumps({"lesson_goal": args.goal,
                        "source_candidates": candidates,
                        "probe_scope": "仅验证给定解释目标下的原问优先，不代表真实课堂已讲或学生已完成。"},
                        ensure_ascii=False)}],
                    lambda a, m, d: feynman_agent.question(a, FeynmanInput.from_messages(m), d,
                        output_validator=lambda _, out: (
                            validate_source_choices([out], candidates, out.source_rejections,
                                                    feynman=True) or out)), [material])
                return 1 if failed_stages else 0

            def plan_validate(_, output):
                repair_outline_candidate(output)
                return validate_plan_sources(output, catalog)

            planning_messages = [
                {"role": "system", "content": state.prompts.load_local_template(
                    "outline_generate_v1")["template"] + "\n" + SOURCE_POLICY},
                {"role": "user", "content": json.dumps({"goal": args.goal,
                    "chapters": catalog, "untrusted_material": excerpts,
                    "constraints": "生成覆盖选定材料主要知识块的期末速成课程。重点详讲，选学内容安排轻量讲次；不把选学等同于删除。每讲写具体可检验目标。",
                    "time_budget_minutes": 480}, ensure_ascii=False)}]
            if args.plan_run:
                planned_folder = (ROOT / args.plan_run).resolve()
                if not planned_folder.is_relative_to((ROOT / ".backup").resolve()):
                    parser.error("Seed planning run must remain below .backup.")
                seed = json.loads((planned_folder / "run.json").read_text(encoding="utf8"))
                if seed["goal"] != args.goal or seed["source_owner"] != args.owner:
                    parser.error("Seed goal or owner differs.")
                plan = seed["stages"]["material_outline"]["output"]
                from app.planning.outputs import OutlineCandidate
                plan_validate(None, OutlineCandidate.model_validate(plan))
                progress["planning_seed_sha256"] = hashlib.sha256(
                    json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                progress["selected_lesson_indices"] = args.lesson_index
                save()
            else:
                plan = call("material_outline", "planning", planning_messages,
                    lambda a, m, d: planning_agent.generate_outline(a, OutlinePlanningInput.from_messages(m),
                        d, output_validator=plan_validate), [material])
            pairs = [(f"probe_lesson_{index}", lesson) for index, lesson in enumerate(
                lesson for unit in plan["units"] for lesson in unit["lessons"])]
            with db.session() as sess:
                snapshot = freeze_sources(sess, progress["owner"], [material], pairs)
            (folder / "material-snapshot.json").write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf8")
            if args.stage == "evidence":
                from types import SimpleNamespace

                from app.agents.runner import run_structured_agent
                from app.materials.study_sources import block_text
                from app.teaching.scope_materials import (
                    ScopeEvidenceDecision,
                    ScopeMaterialSearch,
                )
                from pydantic import BaseModel

                class EvidenceResult(BaseModel):
                    summary: dict
                    checkpoint: dict

                index = (args.lesson_index or [0])[0]
                ident, lesson = pairs[index]
                text = "\f".join("\n".join(filter(None, (block_text(b) for b in p["blocks"])))
                                 for p in sorted(parsed["page_results"], key=lambda p: p["page_no"]))
                source_sections = [{**s, "text": text[s["offset"]:s["end_offset"]]}
                                   for s in catalog if s["id"] in lesson["source_section_ids"]]
                evidence_context = {"course_title": args.goal, "source_snapshot": snapshot,
                    "lessons": [{"lesson_id": ident, "title": lesson["title"], "goal": lesson["goal"]}],
                    "source_sections": source_sections,
                    "required_section_ids": [s["id"] for s in source_sections]}
                evidence_prompt = state.prompts.load_local_template("unit_scope_evidence_v1")["template"]

                def probe(adapter, messages, deps):
                    runs = []

                    def save_search(value):
                        progress["evidence_checkpoint"] = value
                        save()

                    search = ScopeMaterialSearch(state.teaching, progress["owner"], deps.task_id,
                        evidence_context, deps, checkpoint=progress.get("evidence_checkpoint"),
                        save=save_search)

                    def decide(context):
                        def validate(_, output):
                            from pydantic_ai.exceptions import ModelRetry
                            allowed = {s["id"] for s in [*context["source_sections"],
                                *context.get("retrieved_source_sections", [])]}
                            if (not set(output.evidence_section_ids) <= allowed
                                    or output.sufficient and not output.evidence_section_ids):
                                raise ModelRetry("充分性依据须来自已提供的真实章节。")
                            return output

                        run = run_structured_agent(adapter, ScopeEvidenceDecision, [
                            {"role": "system", "content": evidence_prompt},
                            {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                            deps=deps, output_validator=validate, max_requests=3,
                            timeout_seconds=300, agent_name="scope_evidence_review")
                        runs.append(run)
                        return run.payload

                    summary = search.prepare(decide)
                    return SimpleNamespace(payload=EvidenceResult(summary=summary, checkpoint=search.state),
                                           requests=sum(run.requests for run in runs))

                call("scope_evidence_probe", "planning", [
                    {"role": "system", "content": evidence_prompt},
                    {"role": "user", "content": json.dumps(evidence_context, ensure_ascii=False)}],
                    probe, [material])
                return 1 if failed_stages else 0
            from app.agents.contracts import (
                AssessmentInput,
                FeynmanInput,
                TeachingStepInput,
            )
            from app.agents.roles import assessment_agent, feynman_agent, teaching_agent
            from app.teaching.assessment import DRAWING_POLICY, question_issues
            from app.teaching.assessment_sources import figure_catalog
            from app.teaching.assessment_validation import quotas_for
            from app.teaching.board_blocks import GRAPH_EXPRESSION_POLICY
            from app.teaching.examples import (
                ASSESSMENT_EXAMPLE_POLICY,
                EXAMPLE_POLICY,
                assessment_example_issues,
                remaining_candidates,
                validate_teaching_examples,
            )
            all_examples = []
            for lesson_index, (ident, lesson) in enumerate(pairs):
                if args.lesson_index and lesson_index not in args.lesson_index:
                    continue
                with db.session() as sess:
                    candidates = source_candidates(sess, progress["owner"], snapshot,
                        [{"lesson_id": ident}], include_examples=True)
                    fq_candidates = source_candidates(sess, progress["owner"], snapshot,
                        [{"lesson_id": ident}], feynman=True)
                    figures = figure_catalog(sess, progress["owner"], snapshot, ident)
                sources = [section for section in catalog if section["id"] in lesson["source_section_ids"]]
                retrieved = state.retrieval.assemble(owner_user_id=progress["owner"], material_ids=[material],
                    query=lesson["title"] + " " + lesson["goal"], source_sections=sources)
                context = {"course_title": args.goal, "lesson_id": ident,
                    "lesson_title": lesson["title"], "lesson_goal": lesson["goal"],
                    "material_ids": [material], "source_sections": sources,
                    "source_snapshot": snapshot, "example_policy_version": 1,
                    "figure_catalog": figures}
                pdf_targets = []
                if question_folder:
                    from types import SimpleNamespace

                    from app.teaching.interactive import question_targets
                    pdf_targets = question_targets(state.teaching,
                        SimpleNamespace(owner_user_id=progress["owner"], lesson_id=ident), context, candidates)
                    context["pdf_targets"] = pdf_targets
                previous = []
                examples = []
                teaching_failed = False
                for phase in ("concept", "example"):
                    local = {"context": context, "current_stage": {"phase": phase, "goal_ids": ["g1"],
                        "purpose": lesson["goal"]}, "previous": previous, "existing_board": {},
                        "source_candidates": remaining_candidates(candidates, all_examples),
                        "previous_examples": examples, "is_final": False,
                        "selected_material_context": {"excerpts": retrieved.excerpts},
                        "probe_scope": "Representative short segment, not a completed learner lesson."}

                    def validate_step(_, output, phase=phase, candidates=candidates,
                                      pdf_targets=pdf_targets):
                        from pydantic_ai.exceptions import ModelRetry
                        if len(output.steps) != 1 or (output.steps[0].phase and output.steps[0].phase != phase):
                            raise ModelRetry("只生成当前阶段的一个短段。")
                        validate_teaching_examples(output.steps[0], candidates, all_examples, phase)
                        if question_folder:
                            from app.teaching.interactive import bind_source
                            bind_source(output.steps[0].board, pdf_targets)
                            for beat in output.steps[0].beats:
                                bind_source(beat, pdf_targets)
                        return output

                    generated = call(f"{ident}_{phase}", "teaching", [
                        {"role": "system", "content": state.prompts.load_local_template(
                            "teaching_step_v1")["template"] + "\n" + EXAMPLE_POLICY + "\n" + GRAPH_EXPRESSION_POLICY},
                        {"role": "user", "content": json.dumps(local, ensure_ascii=False)}],
                        lambda a, m, d: teaching_agent.generate_step(a,
                            TeachingStepInput.from_messages(m), d, output_validator=validate_step),
                        [material])
                    if generated is None:
                        teaching_failed = True
                        break
                    step = generated["steps"][0]
                    previous.append({"phase": phase, "title": step["title"], "text": step["text"]})
                    for number, example in enumerate(step.get("examples", [])):
                        examples.append({**example, "id": f"{ident}/example/{number}", "lesson_id": ident})
                all_examples.extend(examples)
                if teaching_failed:
                    continue
                feynman = call(f"{ident}_feynman", "teaching", [
                    {"role": "system", "content": state.prompts.load_local_template(
                        "feynman_question_v2")["template"] + "\n" + QUESTION_POLICY},
                    {"role": "user", "content": json.dumps({"lesson_goal": lesson["goal"],
                        "taught": previous, "source_candidates": fq_candidates}, ensure_ascii=False)}],
                    lambda a, m, d, fq_candidates=fq_candidates: feynman_agent.question(a, FeynmanInput.from_messages(m), d,
                        output_validator=lambda _, out, fq_candidates=fq_candidates: (
                            validate_source_choices([out], fq_candidates, out.source_rejections,
                                                    feynman=True) or out)), [material])
                if feynman is None:
                    continue
                eligible = remaining_candidates(candidates, all_examples)
                lesson_context = {"lesson_id": ident, "title": lesson["title"], "goal": lesson["goal"],
                    "knowledge_points": [{"id": "g1", "label": lesson["goal"]}],
                    "taught": previous, "source_candidates": eligible, "figure_catalog": figures,
                    "classroom_examples": examples}
                local = {"lessons": [lesson_context], "lesson_quotas": quotas_for([lesson_context]),
                    "source_candidates": eligible, "classroom_examples": examples,
                    "remaining_original_question_ids": [q["id"] for q in eligible if q["original_allowed"]],
                    "used_classroom_examples": all_examples, "prior_questions": [],
                    "source_selection_probe": True}

                def validate_assessment(_, output, ident=ident, local=local, eligible=eligible):
                    from pydantic_ai.exceptions import ModelRetry
                    issues = question_issues(output, {ident}, local)
                    issues += assessment_example_issues(output, local)
                    if issues:
                        progress.setdefault("validation_failures", []).append({
                            "stage": ident + "_assessment", "issues": issues,
                            "candidate": output.model_dump()})
                        save()
                        raise ModelRetry("；".join(issues))
                    validate_source_choices(output.questions, eligible,
                        output.source_rejections, assessment_version=2)
                    return output

                def generate_assessment(adapter, messages, deps, base_validator=validate_assessment,
                                        local=local):
                    from app.teaching.examples import review_variations, variation_items
                    cache = set()
                    references = {item["id"]: item for item in [
                        *local["source_candidates"], *local["classroom_examples"]]}

                    def validate(ctx, output):
                        base_validator(ctx, output)
                        items = variation_items(output.questions, references, output.source_rejections)
                        key = hashlib.sha256(json.dumps(items, sort_keys=True,
                                                       ensure_ascii=False).encode()).hexdigest()
                        if items and key not in cache:
                            review_variations(adapter, deps, items, {
                                "goal": local["lessons"][0]["goal"],
                                "figure_catalog": local["lessons"][0].get("figure_catalog", {}),
                                "source_candidates": local["source_candidates"]})
                            cache.add(key)
                        return output

                    return assessment_agent.generate_assessment(adapter,
                        AssessmentInput.from_messages(messages), deps, assessment_version=2,
                        output_validator=validate)

                assessment = call(f"{ident}_assessment_reviewed", "teaching", [
                    {"role": "system", "content": state.prompts.load_local_template(
                        "unit_assessment_v2")["template"] + "\n" + QUESTION_POLICY + "\n" +
                        ASSESSMENT_EXAMPLE_POLICY + "\n" + DRAWING_POLICY},
                    {"role": "user", "content": json.dumps(local, ensure_ascii=False)}],
                    generate_assessment, [material])
                progress.setdefault("material_metrics", {})[ident] = {
                    "title": lesson["title"], "candidates": len(candidates),
                    "original_allowed": sum(c["original_allowed"] for c in candidates),
                    "feynman_candidates": len(fq_candidates),
                    "feynman_original_allowed": sum(c["original_allowed"] for c in fq_candidates),
                    "feynman_source_use": feynman["source_use"], "figure_status": figures["status"],
                    "assessment_status": "succeeded" if assessment else "failed", "examples": examples}
                save()
        else:
            print(json.dumps({"stages": list(progress["stages"])}))
    finally:
        db.dispose()
        source_db.dispose()
    return 1 if failed_stages else 0


if __name__ == "__main__":
    raise SystemExit(main())

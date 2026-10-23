"""Explicit paid planning/question probes on an already parsed private document.

Run after document_attachments_files_live.py. Reuses its isolated schema and
completed OCR. All prompts and model outputs remain below the supplied .backup
directory. These probes do not mark any actual lesson as taught or completed.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--topic", default="Simple linear regression: STATS201 selected pages")
    parser.add_argument("--query", default="simple linear regression")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = (root / args.run).resolve()
    if not folder.is_relative_to((root / ".backup").resolve()):
        raise SystemExit("Private output must be below .backup.")
    checkpoint = json.loads((folder / "run.json").read_text(encoding="utf8"))
    if checkpoint.get("schema_cleaned"):
        raise SystemExit("Private schema was cleaned; inspect the saved outputs or start a new live run.")
    schema = checkpoint["schema"]
    if not schema.startswith("document_files_") or len(schema) != 47:
        raise SystemExit("Unexpected private sample schema.")
    sys.path.insert(0, str(root / "server/backend"))
    from dotenv import load_dotenv
    load_dotenv(root / "server/.env")
    os.environ.update(CONTENT_TRACE_ENABLED="0", OTEL_ENABLED="0", PYDANTIC_AI_NO_BANNER="1")
    from app.logging_audit.sdk_logging import install_sdk_logging_safety
    install_sdk_logging_safety()
    from sqlalchemy.engine import make_url
    from app.logging_audit.db import Database, get_database_url
    from app.logging_audit.repositories import PgAuditLog, PgTaskEventStore
    from app.slice1.config_service import ConfigService
    from app.slice1.disclosure_service import DisclosureService
    from app.slice3_app import Slice3State, _configured_model_descriptor
    from app.agents.model_routing import role_config
    from app.agents.task_binding import bind_task_deps
    from app.agents.runner import deps_for_source
    from app.agents.roles import planning_agent, feynman_agent, assessment_agent
    from app.agents.contracts import OutlinePlanningInput, FeynmanInput, AssessmentInput
    from app.materials.study_sources import (SOURCE_POLICY, QUESTION_POLICY, documents,
        chapter_catalog, validate_plan_sources, freeze_sources, source_candidates,
        validate_source_choices, attach_provenance)
    from app.planning.models import CourseRow, PlanningTaskStateRow, ConversationRow
    from app.planning.outputs import OutlineCandidate, repair_outline_candidate
    from app.teaching.assessment import question_issues
    from app.teaching.learning_outputs import UnitAssessment
    from app.teaching.feynman_outputs import FeynmanQuestion
    from app.tasks.service import TaskService
    from pydantic_ai.exceptions import ModelRetry
    from sqlalchemy import select

    source_db = Database()
    source_configs = ConfigService(source_db)
    db = Database(make_url(get_database_url()).update_query_dict(
        {"options": "-csearch_path=" + schema}).render_as_string(hide_password=False))
    audit = PgAuditLog(db)
    state = Slice3State(db, audit)
    db.create_all()
    configs = ConfigService(db, audit=audit)
    disclosures = DisclosureService(db, audit)
    tasks = TaskService(db, PgTaskEventStore(db))
    owner = checkpoint["owner"]
    material_ids = [item["material_id"] for item in checkpoint["documents"]]
    progress_path = folder / "study-probes.json"
    progress = json.loads(progress_path.read_text(encoding="utf8")) if progress_path.exists() else {"stages": {}}
    if progress.get("topic", args.topic) != args.topic:
        raise SystemExit("Probe topic changed; do not reuse earlier results.")
    progress["topic"] = args.topic

    def save():
        progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf8")

    def stage(name, role, kind, input_factory, invoke):
        prior = progress["stages"].get(name)
        if prior and prior["status"] == "completed":
            return prior["output"]
        if prior and not args.retry_failed:
            raise SystemExit("Prior model request is unresolved or failed; explicit --retry-failed is required.")
        if prior:
            progress.setdefault("attempt_history", []).append({"stage": name, **prior})
        view = role_config(source_configs, source_owner, role)
        if not view:
            raise SystemExit("A configured personal model is required for this probe.")
        copied = configs.create(actor_id=owner, is_admin=False, kind="content_model",
            protocol=view["protocol"], endpoint=view["endpoint"], model_name=view["model_name"],
            credentials=source_configs.resolve_credentials(view["config_id"]),
            reasoning_settings=view.get("reasoning_settings", {}))
        subject = progress["course_id"] if kind == "outline_generate" else progress["conversation_id"]
        task = tasks.create_task(owner_user_id=owner, kind=kind, subject_id=subject,
                                 idempotency_key="probe-" + uuid.uuid4().hex, stage="generate")
        if kind == "outline_generate":
            with db.session() as sess:
                sess.add(PlanningTaskStateRow(task_id=task["task_id"], owner_user_id=owner,
                    target_kind="course", target_id=progress["course_id"],
                    material_ids_json=json.dumps(material_ids)))
        lease = tasks.claim(task["task_id"], lease_owner="study-probe", lease_seconds=600)
        scope, category = {"material_ids": sorted(material_ids)}, "material"
        disclosures.create(actor_id=owner, task_id=task["task_id"], config_id=copied["config_id"],
            config_version=copied["config_version"], content_category=category, scope_snapshot=scope)
        adapter = _configured_model_descriptor(copied, configs.resolve_credentials(copied["config_id"]))
        adapter.agent_deps = bind_task_deps(db, adapter, actor_id=owner,
            task_id=task["task_id"], disclosures=disclosures)
        original = adapter.agent_deps.check_state
        def check():
            original()
            current = source_configs.get(view["config_id"], actor_id=source_owner, is_admin=False)
            if not current["is_active"] or current["config_version"] != view["config_version"]:
                raise RuntimeError("source_model_configuration_changed")
        adapter.agent_deps = replace(adapter.agent_deps, check_state=check)
        progress["stages"][name] = {"status": "started", "task_id": task["task_id"],
            "source_config_id": view["config_id"], "source_version": view["config_version"]}
        save()
        started = time.monotonic()
        try:
            output = invoke(adapter, input_factory(adapter))
            result = output.payload.model_dump()
            progress["stages"][name].update(status="completed", seconds=round(time.monotonic() - started, 2),
                                           output=result)
            tasks.submit_result(lease, stage="generate", status="succeeded")
            save()
            print(json.dumps({"stage": name, "status": "completed",
                              "seconds": progress["stages"][name]["seconds"]}), flush=True)
            return result
        except Exception as exc:
            progress["stages"][name].update(status="failed", error_type=type(exc).__name__,
                                           error_code=getattr(exc, "code", "INTERNAL_ERROR"),
                                           seconds=round(time.monotonic() - started, 2))
            save()
            tasks.submit_result(lease, stage="generate", status="failed",
                                error_code=getattr(exc, "code", "INTERNAL_ERROR"))
            print(json.dumps({"stage": name, "status": "failed", "error_type": type(exc).__name__}), flush=True)
            return None
        finally:
            close = getattr(adapter, "close", None)
            if close:
                close()

    try:
        from app.slice1.models import ServiceConfigRow
        with source_db.session() as sess:
            original_parser = sess.get(ServiceConfigRow, checkpoint["source_config_id"])
            source_owner = original_parser.owner_user_id
        if "course_id" not in progress:
            progress["course_id"] = "probe_" + uuid.uuid4().hex[:16]
            progress["conversation_id"] = "probe_" + uuid.uuid4().hex[:16]
            with db.session() as sess:
                sess.add(CourseRow(course_id=progress["course_id"], owner_user_id=owner,
                    title_ref=args.topic))
                sess.add(ConversationRow(conversation_id=progress["conversation_id"],
                    owner_user_id=owner, kind="chat", material_snapshot_json=json.dumps(material_ids)))
            save()
        with db.session() as sess:
            catalog = chapter_catalog(documents(sess, owner, material_ids))
        policy = (SOURCE_POLICY + "\n只规划本次上传资料实际覆盖的学习主题；最多一个单元、两讲，"
                  "不把目录中出现但未上传的其它章当作已有正文。")
        def planning_input(adapter):
            excerpts = state.retrieval.assemble(owner_user_id=owner, material_ids=material_ids,
                                                 query=args.query).excerpts
            return OutlinePlanningInput.from_messages([{"role": "system", "content": policy},
                {"role": "user", "content": json.dumps({"chapters": catalog,
                    "topic": args.topic, "untrusted_material": excerpts, "time_budget_minutes": 90}, ensure_ascii=False)}])
        plan = stage("planning", "planning", "outline_generate", planning_input,
            lambda a, inp: planning_agent.generate_outline(a, inp, deps_for_source(a, max_requests=4),
                output_validator=lambda ctx, out: validate_plan_sources(out, catalog)))
        if not plan:
            return 1
        candidate = OutlineCandidate.model_validate(plan)
        # Match production's deterministic missing-goal repair; record its use.
        repairs = repair_outline_candidate(candidate)
        progress["planning_repairs"] = repairs
        save()
        pairs = [(f"probe_lesson_{i}", lesson.model_dump()) for i, lesson in enumerate(
            (l for unit in candidate.units for l in unit.lessons))]
        with db.session() as sess:
            frozen = freeze_sources(sess, owner, material_ids, pairs)
            candidates = source_candidates(sess, owner, frozen, [{"lesson_id": ident} for ident, _ in pairs])
            feynman_candidates = source_candidates(sess, owner, frozen,
                [{"lesson_id": ident} for ident, _ in pairs], feynman=True)
        # This is a source selection probe; supplied source excerpts are not a
        # claim that a learner has attended or completed these lessons.
        context = {"lessons": [{"lesson_id": ident, "title": lesson["title"], "goal": lesson["goal"],
                    "taught": [{"text": "Source-selection probe scope: " + lesson["goal"]}]}
                   for ident, lesson in pairs], "source_candidates": candidates,
                   "source_selection_probe": True}
        def feynman_input(adapter):
            return FeynmanInput.from_messages([{"role": "system", "content": QUESTION_POLICY +
                "\n这是资料选题实验。生成一个费曼首问和 criteria；仅检查提供资料中已规划的原理。"},
                {"role": "user", "content": json.dumps({**context, "source_candidates": feynman_candidates},
                                                     ensure_ascii=False)}])
        fq = stage("feynman", "teaching", "chat_answer", feynman_input,
            lambda a, inp: feynman_agent.question(a, inp, deps_for_source(a, max_requests=4),
                output_validator=lambda ctx, out: (validate_source_choices(
                    [out], feynman_candidates, out.source_rejections, feynman=True) or out)))
        if fq:
            parsed_question = FeynmanQuestion.model_validate(fq)
            validate_source_choices([parsed_question], feynman_candidates,
                                    parsed_question.source_rejections, feynman=True)
        def assessment_input(adapter):
            return AssessmentInput.from_messages([{"role": "system", "content": QUESTION_POLICY +
                "\n这是资料选题实验，生成3至4道题。原题以short开放题保留题干；"
                "至少覆盖每个给定讲次，评分依据自己拟定，避免依赖未提供的数据。"},
                {"role": "user", "content": json.dumps(context, ensure_ascii=False)}])
        def validate(ctx, out):
            try:
                issues = question_issues(out, {ident for ident, _ in pairs})
                if issues:
                    raise ModelRetry("；".join(issues))
                validate_source_choices(out.questions, candidates, out.source_rejections)
            except ModelRetry as exc:
                progress.setdefault("validation_failures", []).append({
                    "reason": str(exc), "candidate": out.model_dump()})
                save()
                raise
            return out
        assessment = stage("assessment", "teaching", "chat_answer", assessment_input,
            lambda a, inp: assessment_agent.generate_assessment(a, inp, deps_for_source(a, max_requests=4),
                                                                output_validator=validate))
        if assessment:
            validate(None, UnitAssessment.model_validate(assessment))
        summary = {"sample_pages": sum(d.get("metrics", {}).get("input_pages", 0) or 0
                                      for d in checkpoint["documents"]),
                   "chapters": len(catalog), "candidate_questions": len(candidates),
                   "planning_completed": bool(plan), "feynman_completed": bool(fq),
                   "assessment_completed": bool(assessment), "full_teaching_e2e": False}
        if fq:
            summary["feynman_source"] = attach_provenance(FeynmanQuestion.model_validate(fq), feynman_candidates)
        if assessment:
            summary["assessment_sources"] = [attach_provenance(q, candidates)
                                             for q in UnitAssessment.model_validate(assessment).questions]
        (folder / "study-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8")
        print(json.dumps({k: v for k, v in summary.items() if k not in ("feynman_source", "assessment_sources")}),
              flush=True)
        return 0 if fq and assessment else 1
    finally:
        db.dispose()
        source_db.dispose()


if __name__ == "__main__":
    sys.exit(main())

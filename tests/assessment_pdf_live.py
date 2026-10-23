"""Explicit private PDF assessment probe; never marks real lessons completed.

Requires a completed document_attachments_files_live.py run. Uses its private
schema and OCR. Source text, prompts, outputs and credentials never enter fixtures.
"""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--role", choices=("planning", "teaching"), default="planning")
    parser.add_argument("--grade-reference", action="store_true")
    parser.add_argument("--reuse-candidate", action="store_true",
                        help="Revalidate a retained real candidate before issuing another paid request.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = (root / args.run).resolve()
    if not folder.is_relative_to((root / ".backup").resolve()):
        raise SystemExit("Private output must stay below .backup.")
    checkpoint = json.loads((folder / "run.json").read_text(encoding="utf8"))
    schema = checkpoint["schema"]
    if not schema.startswith("document_files_") or len(schema) != 47:
        raise SystemExit("Unexpected private OCR schema.")
    sys.path.insert(0, str(root / "server/backend"))
    from dotenv import load_dotenv
    load_dotenv(root / "server/.env")
    os.environ.update(CONTENT_TRACE_ENABLED="0", OTEL_ENABLED="0", PYDANTIC_AI_NO_BANNER="1")
    from app.logging_audit.sdk_logging import install_sdk_logging_safety
    install_sdk_logging_safety()
    from sqlalchemy.engine import make_url
    from app.logging_audit.db import Database, get_database_url
    from app.logging_audit.repositories import PgAuditLog
    from app.slice3_app import Slice3State, _configured_model_descriptor
    from app.slice1.config_service import ConfigService
    from app.slice1.models import ServiceConfigRow
    from app.slice1.disclosure_service import DisclosureService
    from app.materials.models import DocumentAttachmentRow
    from app.materials.study_sources import build_study_index, documents, freeze_sources, source_candidates
    from app.planning.models import CourseRow, OutlineVersionRow, CourseUnitRow, LessonRow
    from app.teaching.learning_models import UnitAssessmentRow
    from app.teaching.assessment_sources import figure_catalog
    from app.agents.task_binding import bind_task_deps, _domain_binding
    from app.agents.model_routing import role_config
    from app.agents.roles import assessment_agent, evaluation_agent
    from app.tasks.service import LearningTaskRow
    source_db = Database()
    source_configs = ConfigService(source_db)
    db = Database(make_url(get_database_url()).update_query_dict(
        {"options": "-csearch_path=" + schema}).render_as_string(hide_password=False))
    audit = PgAuditLog(db)
    state = Slice3State(db, audit)
    db.create_all()
    configs = ConfigService(db, audit=audit)
    disclosures = DisclosureService(db, audit)
    owner = checkpoint["owner"]
    suffix = "" if args.role == "planning" else "-" + args.role
    progress_path = folder / ("assessment-v2-probe" + suffix + ".json")
    progress = json.loads(progress_path.read_text(encoding="utf8")) if progress_path.exists() else {}
    def save():
        progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf8")
    with source_db.session() as sess:
        source_owner = sess.get(ServiceConfigRow, checkpoint["source_config_id"]).owner_user_id
    original = role_config(source_configs, source_owner, args.role)
    if not original:
        raise SystemExit("No personal planning model is configured.")
    if progress.get("source_version") and (
            progress["source_config_id"] != original["config_id"] or progress["source_version"] != original["config_version"]):
        raise SystemExit("Source model changed; use a separate probe.")
    if "config_id" not in progress:
        copied = configs.create(actor_id=owner, is_admin=False, kind="content_model",
            protocol=original["protocol"], endpoint=original["endpoint"], model_name=original["model_name"],
            credentials=source_configs.resolve_credentials(original["config_id"]),
            reasoning_settings=original.get("reasoning_settings", {}))
        progress.update(config_id=copied["config_id"], source_config_id=original["config_id"],
                        source_version=original["config_version"])
        save()
    adapters = []
    def factory(actor, task_id):
        view = configs.get(progress["config_id"], actor_id=owner, is_admin=False)
        adapter = _configured_model_descriptor(view, configs.resolve_credentials(view["config_id"]))
        adapter.agent_deps = bind_task_deps(db, adapter, actor_id=owner,
                                          task_id=task_id, disclosures=disclosures)
        bound_check = adapter.agent_deps.check_state
        def guard():
            if bound_check:
                bound_check()
            actual = source_configs.get(original["config_id"], actor_id=source_owner, is_admin=False)
            if not actual["is_active"] or actual["config_version"] != original["config_version"]:
                raise RuntimeError("source_model_changed")
        adapter.agent_deps = replace(adapter.agent_deps, check_state=guard)
        adapters.append(adapter)
        return adapter
    state.teaching.model_adapter_factory = factory
    compute = state.assessments._compute
    def recover_candidate(task_id, lease=None):
        if task_id == progress["task_id"] and args.reuse_candidate and progress.get("validation_failures"):
            from app.teaching.learning_outputs import LessonAssessment
            from app.teaching.assessment import question_issues
            from app.materials.study_sources import validate_source_choices
            candidate = LessonAssessment.model_validate(progress["validation_failures"][-1]["candidate"])
            with db.session() as sess:
                row = sess.get(UnitAssessmentRow, progress["assessment_id"])
                frozen = json.loads(row.context_json)
            key = candidate.lesson_quotas[0].lesson_id
            lesson = next(l for l in frozen["lessons"] if l["lesson_id"] == key)
            local = {"lessons": [lesson], "lesson_quotas": [q for q in frozen["lesson_quotas"]
                if q["lesson_id"] == key]}
            issues = question_issues(candidate, {key}, local)
            if issues:
                raise RuntimeError("retained_candidate_still_invalid")
            validate_source_choices(candidate.questions, lesson["source_candidates"],
                candidate.source_rejections, assessment_version=2)
            def restore(value):
                value.setdefault("_batches", {}).setdefault(key, candidate.model_dump())
            state.assessments._checkpoint(task_id, lease, update=restore)
            progress["revalidated_retained_candidate"] = True
            save()
        return compute(task_id, lease)
    state.assessments._compute = recover_candidate
    generate = assessment_agent.generate_assessment
    evaluate = evaluation_agent.evaluate_answers
    def inspect_generate(*values, **options):
        validate = options.get("output_validator")
        def record_validation(ctx, output):
            try:
                return validate(ctx, output)
            except Exception as exc:
                progress.setdefault("validation_failures", []).append({
                    "candidate": output.model_dump(), "reason": str(exc)})
                save()
                raise
        if validate:
            options["output_validator"] = record_validation
        try:
            return generate(*values, **options)
        except Exception as exc:
            chain, cause = [], exc
            for _ in range(8):
                if cause is None:
                    break
                record = {"type": type(cause).__name__}
                if callable(getattr(cause, "errors", None)):
                    record["errors"] = cause.errors(include_url=False, include_context=False)
                chain.append(record)
                cause = cause.__cause__
            progress.setdefault("private_errors", []).append(chain)
            save()
            raise
    assessment_agent.generate_assessment = inspect_generate
    def inspect_evaluate(*values, **options):
        validate = options.get("output_validator")
        def record_validation(ctx, output):
            try:
                return validate(ctx, output)
            except Exception as exc:
                progress.setdefault("grading_validation_failures", []).append({
                    "candidate": output.model_dump(), "reason": str(exc)})
                save()
                raise
        if validate:
            options["output_validator"] = record_validation
        return evaluate(*values, **options)
    evaluation_agent.evaluate_answers = inspect_evaluate
    try:
        if "assessment_id" not in progress:
            material_ids = [d["material_id"] for d in checkpoint["documents"]]
            with db.session() as sess:
                for ident in material_ids:
                    attachment = sess.get(DocumentAttachmentRow, ident)
                    parsed = json.loads(attachment.result_json)
                    parsed["study_index"] = build_study_index(parsed)
                    attachment.result_json = json.dumps(parsed, ensure_ascii=False)
            with db.session() as sess:
                docs = documents(sess, owner, material_ids)
                selected = [(d, s) for d in docs for s in d["index"]["sections"]
                            if "计算题" in s["title"] or "证明题" in s["title"]]
                if not selected:
                    raise SystemExit("No calculation/proof sections were indexed.")
                course, outline, unit = ("probe_" + uuid.uuid4().hex[:16] for _ in range(3))
                lesson_prefix = "probe_l" + uuid.uuid4().hex[:8]
                lessons = [{"lesson_id": lesson_prefix + "_" + str(i), "title": section["title"],
                    "goal": "完成本节原有计算与证明任务，不转换成概念问答。",
                    "source_section_ids": [doc["material_id"] + "/" + section["id"]]}
                    for i, (doc, section) in enumerate(selected)]
                snapshot = freeze_sources(sess, owner, material_ids, [(l["lesson_id"], l) for l in lessons])
                sess.add(CourseRow(course_id=course, owner_user_id=owner, title_ref="Private source-selection probe"))
                sess.flush()
                sess.add(OutlineVersionRow(outline_version_id=outline, course_id=course,
                    source_snapshot_json=json.dumps(snapshot)))
                sess.flush()
                sess.get(CourseRow, course).current_outline_version_id = outline
                sess.add(CourseUnitRow(unit_id=unit, outline_version_id=outline, title_ref="Private sample"))
                sess.flush()
                quotas = []
                for i, lesson in enumerate(lessons):
                    sess.add(LessonRow(lesson_id=lesson["lesson_id"], outline_version_id=outline,
                        unit_id=unit, order_index=i, title_ref=lesson["title"], goal_ref=lesson["goal"]))
                    lesson["source_candidates"] = source_candidates(sess, owner, snapshot, [lesson])
                    lesson["figure_catalog"] = figure_catalog(sess, owner, snapshot, lesson["lesson_id"])
                    lesson["knowledge_points"] = []
                    # This is an explicitly labelled source-selection probe,
                    # not evidence of a learner attending these lessons.
                    lesson["taught"] = [{"phase": "example", "text": c["prompt"]}
                                        for c in lesson["source_candidates"]]
                    proof = "证明题" in lesson["title"]
                    quotas.append({"lesson_id": lesson["lesson_id"], "importance": "light" if proof else "core",
                        "target_questions": 5 if proof else 10, "rationale": "私有抽页选题实验，保留原计算与证明要求。"})
            task = state.tasks.create_task(owner_user_id=owner, kind="unit_assessment_generate",
                subject_id=course, idempotency_key="private-probe-" + uuid.uuid4().hex,
                stage="generate", deadline_seconds=1800)
            context = {"assessment_version": 2, "source_selection_probe": True, "unit_title": "Private sample",
                "lessons": lessons, "lesson_quotas": quotas, "source_snapshot": snapshot}
            ident = "assessment_" + uuid.uuid4().hex[:16]
            with db.session() as sess:
                sess.add(UnitAssessmentRow(assessment_id=ident, owner_user_id=owner, course_id=course,
                    unit_id=unit, outline_version_id=outline, task_id=task["task_id"],
                    context_json=json.dumps(context, ensure_ascii=False)))
            progress.update(assessment_id=ident, task_id=task["task_id"])
            state.assessments._bind(task["task_id"], "unit_assessment_v2")
            save()
        task_id = progress["task_id"]
        with db.session() as sess:
            task = sess.get(LearningTaskRow, task_id)
            scope, _, category = _domain_binding(sess, task)
        if not progress.get("grant_created"):
            disclosures.create(actor_id=owner, task_id=task_id, config_id=progress["config_id"],
                config_version=1, content_category=category, scope_snapshot=scope)
            progress["grant_created"] = True
            save()
        task = state.tasks.get_task(task_id)
        if task["status"] == "failed":
            if not args.retry_failed:
                raise SystemExit("Prior probe failed; explicit --retry-failed required.")
            state.tasks.requeue_for_retry(task_id, actor_id=owner)
        if task["status"] != "succeeded":
            state.assessments._run(task_id)
        task = state.tasks.get_task(task_id)
        progress["status"] = task["status"]
        with db.session() as sess:
            row = sess.get(UnitAssessmentRow, progress["assessment_id"])
            result = json.loads(row.questions_json)
        progress["result"] = result
        save()
        summary = {"status": task["status"], "error_code": task.get("error_code"),
            "role": args.role, "model": original["model_name"],
            "questions": len(result.get("questions", [])), "completed_lessons": (
                len(result.get("lesson_quotas", [])) if task["status"] == "succeeded"
                else len(result.get("_batches", {}))),
            "sources": {kind: sum(q["source"]["kind"] == kind for q in result.get("questions", []))
                        for kind in ("original", "adapted", "generated")},
            "full_teaching_e2e": False}
        if task["status"] == "succeeded" and args.grade_reference:
            state.assessments.kick = lambda *_: None
            answers = {q["id"]: q["answer"] if q["kind"] in ("single", "multiple")
                       else "\n".join(q["answer"]) for q in result["questions"]}
            submitted = state.assessments.submit(progress["assessment_id"], owner, answers, "private-reference-replay-v2")
            with db.session() as sess:
                grade_task = sess.get(LearningTaskRow, submitted["task_id"])
                grade_scope, _, grade_category = _domain_binding(sess, grade_task)
            if submitted["task_id"] not in progress.get("grade_grants", []):
                disclosures.create(actor_id=owner, task_id=submitted["task_id"], config_id=progress["config_id"],
                    config_version=1, content_category=grade_category, scope_snapshot=grade_scope)
                progress.setdefault("grade_grants", []).append(submitted["task_id"])
                save()
            grade_task = state.tasks.get_task(submitted["task_id"])
            if grade_task["status"] == "failed" and args.retry_failed:
                state.tasks.requeue_for_retry(submitted["task_id"], actor_id=owner)
                grade_task = state.tasks.get_task(submitted["task_id"])
            if grade_task["status"] in ("queued", "expired"):
                state.assessments._run(submitted["task_id"])
            graded = state.assessments.attempt(submitted["attempt_id"], owner)
            progress["reference_replay"] = graded
            summary["reference_replay_status"] = graded["status"]
            summary["reference_replay_is_independent_quality_test"] = False
            save()
        (folder / ("assessment-v2-summary" + suffix + ".json")).write_text(json.dumps(summary, indent=2), encoding="utf8")
        print(json.dumps(summary))
        return 0 if task["status"] == "succeeded" and (
            not args.grade_reference or summary.get("reference_replay_status") == "succeeded") else 1
    finally:
        state.assessments._compute = compute
        assessment_agent.generate_assessment = generate
        evaluation_agent.evaluate_answers = evaluate
        for adapter in adapters:
            if callable(getattr(adapter, "close", None)):
                adapter.close()
        db.dispose()
        source_db.dispose()


if __name__ == "__main__":
    sys.exit(main())

"""Explicit real-provider scope generation, with held-out exams excluded by design."""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid


def load_goal_outline(payload):
    from pydantic import BaseModel, ConfigDict, Field

    class LessonGoal(BaseModel):
        model_config = ConfigDict(extra="forbid")
        title: str = Field(min_length=1, max_length=200, pattern=r"\S")
        goal: str = Field(min_length=1, max_length=600, pattern=r"\S")

    class UnitGoals(BaseModel):
        model_config = ConfigDict(extra="forbid")
        title: str = Field(min_length=1, max_length=200, pattern=r"\S")
        lessons: list[LessonGoal] = Field(min_length=1, max_length=40)

    class CourseGoals(BaseModel):
        model_config = ConfigDict(extra="forbid")
        title: str = Field(min_length=1, max_length=200, pattern=r"\S")
        units: list[UnitGoals] = Field(min_length=1, max_length=40)

    return CourseGoals.model_validate_json(payload).model_dump()


def resume_configuration(configs, owner, progress):
    if progress.get("source_owner") != owner:
        raise SystemExit("The experiment belongs to another owner.")
    pinned = configs.get(progress["source_config_id"], actor_id=owner, is_admin=False)
    if (not pinned["is_active"] or pinned["config_version"] != progress["source_config_version"]
            or pinned["kind"] != "content_model"):
        raise SystemExit("The pinned experiment configuration is no longer valid.")
    return pinned


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--source", help="Independent Markdown learning material.")
    inputs.add_argument("--outline", help="Independent JSON course goals; no learning material.")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--course-title")
    parser.add_argument("--reuse-course-from",
                        help="Reuse an owned private experiment course under a new policy in a new output folder.")
    parser.add_argument("--unit", type=int, action="append",
                        help="Generate only selected unit numbers; an incomplete run cannot freeze.")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--keep-going", action="store_true",
                        help="Record failed units and continue independent units; never freeze a partial run.")
    parser.add_argument("--evaluation-mode", choices=("held-out", "known-baseline"), default="held-out")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = (root / args.out).resolve()
    if not folder.is_relative_to((root / ".backup").resolve()):
        raise SystemExit("Private output must remain under .backup.")
    source = Path(args.source or args.outline)
    if args.source and source.suffix.lower() != ".md":
        raise SystemExit("This probe accepts one independent Markdown source only.")
    payload = source.read_bytes()
    goal_outline = load_goal_outline(payload) if args.outline else None
    input_kind = "goals" if goal_outline else "material"
    folder.mkdir(parents=True, exist_ok=True)
    from dotenv import load_dotenv
    load_dotenv(root / "server/.env")
    os.environ.update(CONTENT_TRACE_ENABLED="0", OTEL_ENABLED="0", PYDANTIC_AI_NO_BANNER="1")
    sys.path.insert(0, str(root / "server/backend"))
    from app.logging_audit.sdk_logging import install_sdk_logging_safety
    install_sdk_logging_safety()
    from sqlalchemy import create_engine, text, select
    from sqlalchemy.engine import make_url
    from app.logging_audit.db import Database, get_database_url
    from app.logging_audit.repositories import PgAuditLog
    from app.slice3_app import Slice3State, _configured_model_descriptor
    from app.slice1.config_service import ConfigService
    from app.slice1.disclosure_service import DisclosureService
    from app.agents.model_routing import role_config, TaskModelRoleRow
    from app.agents.task_binding import bind_task_deps, AgentBudgetRow
    from app.materials.local_documents import parse_text
    from app.materials.models import MaterialRow, DocumentAttachmentRow
    from app.materials.study_sources import documents, chapter_catalog, freeze_sources, digest
    from app.planning.models import CourseRow, OutlineVersionRow, CourseUnitRow, LessonRow
    from app.teaching.unit_scope import UnitScopeRow
    original_db = Database()
    original_configs = ConfigService(original_db)
    state_path = folder / "run.json"
    progress = json.loads(state_path.read_text(encoding="utf8")) if state_path.exists() else {}
    if not progress and args.reuse_course_from:
        previous = (root / args.reuse_course_from).resolve()
        if not previous.is_relative_to((root / ".backup").resolve()) or previous == folder:
            raise SystemExit("Seed course must be another private experiment.")
        progress = json.loads((previous / "run.json").read_text(encoding="utf8"))
        progress["units"] = [{"unit_id": unit["unit_id"], "title": unit["title"]}
                             for unit in progress["units"]]
        progress["evaluation_mode"] = args.evaluation_mode
    original = (resume_configuration(original_configs, args.owner, progress) if progress else
                role_config(original_configs, args.owner, "planning"))
    if not original:
        raise SystemExit("The selected owner has no active planning model.")
    source_hash = hashlib.sha256(payload).hexdigest()
    if progress and (progress["source_sha256"] != source_hash or
                     progress.get("input_kind", "material") != input_kind or
                     progress["source_config_id"] != original["config_id"] or
                     progress["source_config_version"] != original["config_version"] or
                     progress.get("evaluation_mode", "held-out") != args.evaluation_mode):
        raise SystemExit("Source or model changed; use a new independent run.")
    def save():
        state_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf8")
    admin = create_engine(make_url(get_database_url()))
    if not progress:
        schema = "unit_scope_live_" + uuid.uuid4().hex
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        progress.update(schema=schema, owner="probe_" + uuid.uuid4().hex[:20],
                        input_kind=input_kind,
                        evaluation_mode=args.evaluation_mode,
                        source_sha256=source_hash, source_config_id=original["config_id"],
                        source_config_version=original["config_version"],
                        source_owner=args.owner, model=original["model_name"], units=[])
        save()
    schema = progress["schema"]
    if not schema.startswith("unit_scope_live_") or len(schema) != 48:
        raise SystemExit("Unexpected private schema.")
    scoped_url = make_url(get_database_url()).update_query_dict(
        {"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
    db = Database(scoped_url)
    audit = PgAuditLog(db)
    state = Slice3State(db, audit)
    # Parallel unit probes may share this schema; serialize trigger installation.
    with db.engine.begin() as initialization:
        initialization.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
                               {"scope": "scope-probe-init:" + schema})
        db.create_all()
    configs, disclosures = ConfigService(db, audit=audit), DisclosureService(db, audit)
    state.teaching.configs, state.teaching.disclosures = configs, disclosures
    owner = progress["owner"]
    if "config_id" not in progress:
        view = configs.create(actor_id=owner, is_admin=False, kind="content_model",
            protocol=original["protocol"], endpoint=original["endpoint"], model_name=original["model_name"],
            credentials=original_configs.resolve_credentials(original["config_id"]),
            reasoning_settings=original.get("reasoning_settings", {}))
        progress["config_id"] = view["config_id"]
        save()
    # The isolated copy has its own evidence cache. Verify streaming rather than
    # copying another configuration's capability claims or forcing availability.
    copied_view = configs.get(progress["config_id"], actor_id=owner, is_admin=False)
    streaming = copied_view["capability_status"].get("streaming", "unknown")
    streaming_state = streaming.get("state") if isinstance(streaming, dict) else streaming
    if streaming_state == "unknown":
        from app.slice1.adapters import CapabilityTestService
        tested = CapabilityTestService(configs).run(
            progress["config_id"], actor_id=owner, is_admin=False, capabilities=["streaming"])
        progress["streaming_probe"] = tested["capability_status"]["streaming"]
        save()
        print(json.dumps({"stage": "capability_probe",
                          "streaming": progress["streaming_probe"]["state"]}), flush=True)
    def factory(actor, task_id):
        view = configs.get(progress["config_id"], actor_id=owner, is_admin=False)
        adapter = _configured_model_descriptor(view, configs.resolve_credentials(view["config_id"]))
        with db.session() as sess:
            binding = sess.get(TaskModelRoleRow, (task_id, "planning"))
            adapter.disclosure_grant_id = binding.grant_id
        adapter.agent_deps = bind_task_deps(db, adapter, actor_id=owner,
                                           task_id=task_id, disclosures=disclosures)
        check = adapter.agent_deps.check_state
        def guarded():
            check()
            actual = original_configs.get(original["config_id"], actor_id=args.owner, is_admin=False)
            if not actual["is_active"] or actual["config_version"] != original["config_version"]:
                from app.planning.errors import error
                raise error("MODEL_CREDENTIAL_INVALID")
        adapter.agent_deps = replace(adapter.agent_deps, check_state=guarded)
        return adapter
    state.teaching.model_adapter_factory = factory
    scopes = state.assessments.scopes
    scopes.kick = lambda _: None
    try:
        if progress["units"]:
            # Never overwrite a frozen earlier experiment after a policy upgrade.
            with db.session() as sess:
                course = sess.get(CourseRow, progress["course_id"])
                requested_title = args.course_title or (goal_outline or {}).get("title")
                if requested_title and requested_title != course.title_ref:
                    raise SystemExit("Course title changed; use a new independent run.")
                for unit in progress["units"]:
                    if not unit.get("scope_id"):
                        continue
                    row = sess.get(UnitScopeRow, unit["scope_id"])
                    bound_unit = sess.get(CourseUnitRow, unit["unit_id"])
                    if row.input_hash != digest(scopes.context(sess, course, bound_unit, owner)):
                        raise SystemExit("Scope policy or input changed; preserve this run and use a new folder.")
        if not progress["units"]:
            parsed = parse_text(payload, "md") if args.source else None
            material_id = "mat_" + uuid.uuid4().hex[:16]
            course_id, outline_id = "course_" + uuid.uuid4().hex[:16], "outline_" + uuid.uuid4().hex[:16]
            with db.session() as sess:
                if parsed:
                    sess.add(MaterialRow(material_id=material_id, owner_user_id=owner,
                        filename_hash=source_hash, source_bytes_sha256=source_hash,
                        kind="md", size_bytes=len(payload), status="parsed"))
                    sess.flush()
                    sess.add(DocumentAttachmentRow(material_id=material_id, owner_user_id=owner,
                        upload_key=uuid.uuid4().hex, task_id="local-source", result_json=json.dumps(parsed, ensure_ascii=False)))
                sess.add(CourseRow(course_id=course_id, owner_user_id=owner,
                                   title_ref=args.course_title or (goal_outline or {}).get("title") or source.stem))
                sess.flush()
                sess.add(OutlineVersionRow(outline_version_id=outline_id, course_id=course_id))
                sess.flush()
                sess.get(CourseRow, course_id).current_outline_version_id = outline_id
                catalog = chapter_catalog(documents(sess, owner, [material_id])) if parsed else []
                if parsed and not catalog:
                    raise RuntimeError("Expected an indexed source with at least one section.")
                top_level = min((section["level"] for section in catalog), default=0)
                chapters = goal_outline["units"] if goal_outline else [
                    section for section in catalog if section["level"] == top_level]
                lesson_pairs = []
                for number, chapter in enumerate(chapters):
                    unit_id = "unit_" + uuid.uuid4().hex[:16]
                    sess.add(CourseUnitRow(unit_id=unit_id, outline_version_id=outline_id,
                        title_ref=chapter["title"], order_index=number))
                    sess.flush()
                    sections = chapter["lessons"] if goal_outline else [
                        section for section in catalog if section["level"] == chapter["level"] + 1
                        and chapter["offset"] <= section["offset"] < chapter["end_offset"]]
                    for index, section in enumerate(sections or [chapter]):
                        lesson_id = "lesson_" + uuid.uuid4().hex[:16]
                        sess.add(LessonRow(lesson_id=lesson_id, outline_version_id=outline_id,
                            unit_id=unit_id, title_ref=section["title"],
                            goal_ref=section["goal"] if goal_outline else
                                "独立完成本节方法的计算、推导与应用，说明适用条件及易错边界。",
                            order_index=index))
                        if parsed:
                            lesson_pairs.append((lesson_id, {"source_section_ids": [section["id"]]}))
                    progress["units"].append({"unit_id": unit_id, "title": chapter["title"]})
                sess.flush()
                frozen = freeze_sources(sess, owner, [material_id], lesson_pairs) if parsed else {}
                sess.get(OutlineVersionRow, outline_id).source_snapshot_json = json.dumps(frozen, ensure_ascii=False)
            progress.update(course_id=course_id, material_id=material_id if parsed else None)
            save()
        failed = False
        for number, unit in enumerate(progress["units"], 1):
            if args.unit and number not in args.unit:
                continue
            existing = scopes.get(progress["course_id"], unit["unit_id"], owner)
            if existing["status"] == "succeeded":
                unit.update({key: existing[key] for key in ("scope_id", "task_id", "scope_hash", "status")})
                (folder / f"scope-{number}.json").write_text(
                    json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf8")
                with db.session() as sess:
                    row = sess.get(UnitScopeRow, existing["scope_id"])
                    (folder / f"input-{number}.json").write_text(row.context_json, encoding="utf8")
                    budget = sess.get(AgentBudgetRow, row.task_id)
                    unit.update(logical_requests=budget.logical_requests, wire_requests=budget.wire_requests)
                save()
                print(json.dumps({"unit": number, "status": "reused"}, ensure_ascii=False), flush=True)
                continue
            if existing["status"] in ("failed", "expired", "leased", "running") and args.retry_failed:
                created = scopes.retry(progress["course_id"], unit["unit_id"], owner)
            elif existing["status"] in ("failed", "cancelled", "expired"):
                print(json.dumps({"unit": number, "status": existing["status"],
                                  "error_code": existing.get("error_code")}), flush=True)
                return 1
            else:
                created = scopes.generate(progress["course_id"], unit["unit_id"], owner)
            unit.update(created)
            save()
            print(json.dumps({"unit": number, "status": "generating"}), flush=True)
            scopes._run(created["task_id"])
            result = scopes.get(progress["course_id"], unit["unit_id"], owner)
            attempts = folder / "attempts"
            attempts.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            (attempts / f"scope-{number}-{stamp}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
            (folder / f"scope-{number}.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
            with db.session() as sess:
                row = sess.get(UnitScopeRow, created["scope_id"])
                (folder / f"input-{number}.json").write_text(row.context_json, encoding="utf8")
                budget = sess.get(AgentBudgetRow, created["task_id"])
                unit.update(status=result["status"], scope_hash=result["scope_hash"],
                    logical_requests=budget.logical_requests, wire_requests=budget.wire_requests)
            save()
            print(json.dumps({"unit": number, "status": result["status"],
                              "points": len((result["scope"] or {}).get("points", [])),
                              "error_code": result.get("error_code")}), flush=True)
            if result["status"] != "succeeded":
                failed = True
                if not args.keep_going:
                    return 1
        if failed or any(unit.get("status") != "succeeded" for unit in progress["units"]):
            return 1
        # Preserve whether optimization has already seen the evaluation baseline.
        manifest = {"source_sha256": source_hash, "model": original["model_name"],
                    "input_kind": input_kind,
                    "unit_hashes": [unit["scope_hash"] for unit in progress["units"]],
                    "baseline_used_for_generation": args.evaluation_mode == "known-baseline",
                    "exam_content_in_generation_input": False,
                    "evaluation_mode": args.evaluation_mode}
        manifest["prompt_bindings"] = [
            {key: binding[key] for key in ("prompt_name", "template_sha256", "source")}
            for binding in (state.prompts.resolve_for_task(unit["task_id"]) for unit in progress["units"])]
        manifest["frozen_hash"] = digest(manifest)
        (folder / "frozen-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf8")
        with db.session() as sess:
            course_title = sess.get(CourseRow, progress["course_id"]).title_ref
        lines = ["# " + course_title + " 独立考纲", "",
                 "由项目 UnitScopeService 通过实际配置模型生成。输入为独立课程目标或学习资料；试卷及答案没有加入生成输入。",
                 "评价模式：" + args.evaluation_mode + "；known-baseline表示优化者已见试卷，不是盲测。",
                 "这是独立考纲生成实验，不代表整门课堂已经生成或测验预热已经接入。", ""]
        for number, unit in enumerate(progress["units"], 1):
            value = json.loads((folder / f"scope-{number}.json").read_text(encoding="utf8"))
            lines += ["## " + unit["title"], "", "冻结哈希：" + value["scope_hash"], ""]
            for point in value["scope"]["points"]:
                lines += ["### " + point["point_id"] + " " + point["label"], "",
                          "归属讲次：" + point["lesson_id"] + "；重要性：" + point["importance"], ""]
                lines += ["- " + capability for capability in point["capabilities"]]
                lines += ["", "条件边界：" + "；".join(point["boundaries"]), ""]
                lines += ["依据类型：" + point.get("basis", "source"),
                          "补充理由：" + point.get("supplement_reason", ""), ""]
                lines += ["依据：" + ref["section_id"] + "，逐字引用：" + ref["quote"]
                          for ref in point["source_refs"]]
                lines.append("")
            if value["scope"]["source_corrections"]:
                lines += ["### 资料条件修正", ""] + [
                    "- " + correction for correction in value["scope"]["source_corrections"]] + [""]
        (folder / "independent-scope.md").write_text("\n".join(lines), encoding="utf8")
        print(json.dumps({"status": "frozen", "units": len(progress["units"]),
                          "frozen_hash": manifest["frozen_hash"]}), flush=True)
        return 0
    finally:
        scopes.pool.shutdown(wait=True)
        db.dispose()
        original_db.dispose()
        admin.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

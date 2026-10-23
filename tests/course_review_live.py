"""Opt-in real-provider review smoke in an owned PostgreSQL schema.

Only a saved teaching connection is read from the business DB. All coursework,
answers, grants and FSRS changes belong to the disposable schema.
"""
import argparse
import json
import os
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "backend"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required")
    from app.logging_audit.sdk_logging import install_sdk_logging_safety
    install_sdk_logging_safety()
    from dotenv import load_dotenv
    load_dotenv(ROOT / "server" / ".env", override=False)
    os.environ.update(CONTENT_TRACE_ENABLED="0", OTEL_ENABLED="0", PYDANTIC_AI_NO_BANNER="1")
    from app.infrastructure.config import settings
    from app.logging_audit.db import Database, get_database_url
    from app.slice1.config_service import ConfigService
    from app.slice1.models import ServiceConfigRow
    from app.agents.model_routing import ModelRoleRow
    from app.auth.models import UserRow
    from sqlalchemy import create_engine, select, text
    from sqlalchemy.engine import make_url

    business = Database()
    try:
        with business.session() as sess:
            selected = sess.scalar(select(ServiceConfigRow).join(ModelRoleRow,
                ModelRoleRow.config_id == ServiceConfigRow.config_id).join(
                    UserRow, UserRow.user_id == ModelRoleRow.owner_user_id).where(
                    ModelRoleRow.role == "teaching", ServiceConfigRow.is_active.is_(True),
                    UserRow.status == "approved").order_by(ServiceConfigRow.created_at.desc()).limit(1))
            if selected is None:
                raise RuntimeError("saved_teaching_configuration_missing")
            owner, ident = selected.owner_user_id, selected.config_id
        configs = ConfigService(business, encryption_key=settings.APP_ENCRYPTION_KEY)
        view = configs.get(ident, actor_id=owner, is_admin=False)
        credentials = configs.resolve_credentials(ident)
    finally:
        business.dispose()

    base = make_url(get_database_url())
    schema = "review_live_" + uuid.uuid4().hex
    admin = create_engine(base)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated_url = base.update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
    os.environ.update(LEARNING_DATABASE_URL=isolated_url, DATABASE_URL=isolated_url)
    db, factory = Database(isolated_url), None
    summary = {"model": view["model_name"], "protocol": view["protocol"], "checks": [],
               "scope": "synthetic teaching and answers, real configured provider; isolated database"}
    started = time.monotonic()
    try:
        from app.slice3_app import Slice3State, _config_model_factory
        from app.logging_audit.repositories import PgAuditLog
        from app.slice1.disclosure_service import DisclosureService
        from app.review.router import AnswerIn, DraftIn
        from app.review.models import ReviewCardRow, ReviewLogRow, ReviewSessionRow
        from app.review.service import ReviewService, digest
        from app.planning.models import (CourseRow, CourseUnitRow, OutlineVersionRow, LessonRow,
            LessonContentVersionRow, ConversationRow, utcnow)
        from app.teaching.models import TeachingStepRow
        from app.teaching.learning_models import LessonBlueprintRow, TaughtKnowledgeRow
        from app.tasks.service import LearningTaskRow
        state = Slice3State(db)
        db.create_all()
        owner, course, outline, lesson, content, conv = ["live_" + uuid.uuid4().hex for _ in range(6)]
        unit = "unit_" + uuid.uuid4().hex
        with db.session() as sess:
            sess.add(UserRow(user_id=owner, email="review-live@example.invalid",
                password_hash="not-a-real-login", status="approved"))
            sess.add(CourseRow(course_id=course, owner_user_id=owner,
                title_ref="合成复习：键值查找与事务", current_outline_version_id=outline))
            sess.flush()
            sess.add(OutlineVersionRow(outline_version_id=outline, course_id=course))
            sess.flush()
            sess.add(CourseUnitRow(unit_id=unit, outline_version_id=outline, title_ref="合成基础"))
            sess.flush()
            sess.add(LessonRow(lesson_id=lesson, unit_id=unit, outline_version_id=outline,
                title_ref="键值查找与事务边界", goal_ref="理解唯一键与事务原子性"))
            sess.flush()
            sess.add(LessonContentVersionRow(content_version_id=content, lesson_id=lesson))
            sess.add(ConversationRow(conversation_id=conv, owner_user_id=owner, course_id=course,
                outline_version_id=outline, lesson_id=lesson, lesson_content_version_id=content, kind="teaching"))
            sess.flush()
            sess.add(LessonBlueprintRow(conversation_id=conv, owner_user_id=owner, lesson_id=lesson,
                outline_version_id=outline, content_version_id=content, plan_json="{}", context_json="{}",
                source_task_id="synthetic"))
            for number, (title, taught) in enumerate([
                ("唯一键查找", "字典使用唯一的键定位对应的值，而不是按位置查找。"
                 "例如键apple对应值3，通过apple读取3；更新同一个键会替换其值，不创建第二个相同键。"),
                ("事务原子性", "事务原子性要求一组数据库修改全部成功，或失败时全部回滚。"
                 "例如转账扣除A账户100元与给B账户增加100元必须同时完成，不能只扣款不入账。")], 1):
                step, record = "step_" + uuid.uuid4().hex, uuid.uuid4().hex * 2
                sess.add(TeachingStepRow(step_id=step, conversation_id=conv, generation=1, step_no=number,
                    title=title, text=taught, status="ready", text_version_no=1))
                sess.add(TaughtKnowledgeRow(record_id=record, owner_user_id=owner, course_id=course,
                    lesson_id=lesson, content_version_id=content, conversation_id=conv,
                    plan_hash=digest("{}"), checkpoint_id=str(number), after_step_no=number,
                    title=title, summary=taught, source_json=json.dumps([{"step_id": step, "text_version": 1}]),
                    completed_at=utcnow()-timedelta(days=3)))
        audit = PgAuditLog(db)
        configs = ConfigService(db, encryption_key=uuid.uuid4().hex, audit=audit)
        config = configs.create(actor_id=owner, is_admin=False, kind="content_model",
            protocol=view["protocol"], endpoint=view["endpoint"], model_name=view["model_name"],
            credentials=credentials, reasoning_settings=view.get("reasoning_settings", {}))
        credentials.clear()
        with db.session() as sess:
            sess.add(ModelRoleRow(owner_user_id=owner, role="teaching", config_id=config["config_id"]))
            row = sess.get(ServiceConfigRow, config["config_id"])
            row.capability_status = json.dumps(view.get("capability_status", {}))
        factory = _config_model_factory(configs)
        # Avoid unrelated automatic capability probes in this bounded live run.
        factory.refresher.register = lambda *args, **kwargs: None
        state.teaching.model_adapter_factory = factory
        state.teaching.configs = configs
        state.teaching.disclosures = DisclosureService(db, audit)
        service = state.review
        service.kick = lambda task_id: None
        session = service.create(owner, course, uuid.uuid4().hex, "Asia/Shanghai")
        service._run(session["task_id"])
        session = service.get(session["session_id"], owner)
        if session["status"] != "ready":
            raise RuntimeError("generation_" + str(session.get("error_code")))
        assert not {"answer", "criteria", "reference"} & set(session["question"])
        summary["checks"].append("real_generation_and_private_projection")
        for number in range(2):
            if number == 0:
                service.draft(session["session_id"], owner, DraftIn(revision=session["revision"],
                    answer="合成草稿", rating=3))
                restored = ReviewService(state.teaching).get(session["session_id"], owner)
                assert restored["draft"]["answer"] == "合成草稿"
                summary["checks"].append("persistent_draft_restore")
                with db.session() as sess:
                    row = sess.get(ReviewSessionRow, session["session_id"])
                    reference = json.loads(row.questions_json)[row.cursor]["answer"]
                answer, expected = reference, "correct"
            else:
                answer, expected = "我不知道，完全不记得这个知识点。", "incorrect"
            value = AnswerIn(revision=session["revision"], answer=answer, rating=4,
                             idempotency_key=uuid.uuid4().hex)
            queued = service.answer(session["session_id"], owner, value)
            service._run(queued["task_id"])
            session = service.get(session["session_id"], owner)
            if session["status"] != "feedback":
                raise RuntimeError("grading_" + str(session.get("error_code")))
            assert session["feedback"]["result"] == expected
            assert session["feedback"]["scheduled"]
            assert session["feedback"]["rating"] == (4 if number == 0 else 1)
            duplicate = service.answer(session["session_id"], owner, value)
            assert duplicate["feedback"] == session["feedback"]
            summary["checks"].append("correct_easy" if number == 0 else "incorrect_forced_again")
            session = service.move(session["session_id"], owner, session["revision"])
        assert session["status"] == "finished"
        with db.session() as sess:
            assert len(list(sess.scalars(select(ReviewLogRow)))) == 2
            assert all(c.revision == 2 for c in sess.scalars(select(ReviewCardRow)))
            summary["task_statuses"] = [t.status for t in sess.scalars(select(LearningTaskRow))]
        summary.update(ok=True, seconds=round(time.monotonic()-started, 2))
    except Exception as exc:
        summary.update(ok=False, failure_type=type(exc).__name__,
                       error_code=getattr(exc, "code", None))
        # Only explicitly controlled labels, never raw provider exceptions.
        if type(exc) is RuntimeError and str(exc).startswith(("generation_", "grading_")):
            summary["stage_error"] = str(exc)
    finally:
        if factory:
            factory.refresher.close()
        db.dispose()
        if not schema.startswith("review_live_") or len(schema) != 44:
            raise RuntimeError("unexpected disposable schema")
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
        summary["isolated_schema_removed"] = True
        destination = ROOT / ".backup" / "course-review"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "live-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "failure_type": type(exc).__name__}))
        raise SystemExit(1) from None

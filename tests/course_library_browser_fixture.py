"""Create synthetic browser fixtures only inside an explicitly isolated PostgreSQL schema."""

import json
import sys
import uuid

from sqlalchemy import text
from app.logging_audit.db import Database
from app.planning.drafts import DraftService
from app.planning.models import CourseRow, LearningRouteRow, PlanningTaskStateRow

db = Database()
with db.engine.connect() as conn:
    assert conn.execute(text("select current_schema()")).scalar().startswith("course_library_validation_")
owner = sys.argv[1]
service = DraftService(db)
payload = {"title": "合成浏览器后端", "stages": [{"title": "基础", "courses": [
    {"title": "合成数据库", "goal": "理解数据", "units": [{"title": "基础单元", "lessons": [
        {"title": "数据表", "goal": "理解数据表"}]}]},
    {"title": "合成操作系统", "goal": "理解进程", "units": [{"title": "进程单元", "lessons": [
        {"title": "进程", "goal": "理解进程调度"}]}]},
]}]}
if len(sys.argv) > 2 and sys.argv[2] == "large":
    for course in payload["stages"][0]["courses"]:
        course["units"] = [{"title": f"合成单元{u}", "lessons": [
            {"title": f"合成讲次{u}-{n}", "goal": "合成学习目标"} for n in range(20)]} for u in range(2)]

def route():
    rid = "route_" + uuid.uuid4().hex[:12]
    with db.session() as sess:
        sess.add(LearningRouteRow(route_id=rid, owner_user_id=owner,
            title_ref=payload["title"], input_ref="合成浏览器学习目标"))
    draft = service.find_or_create_draft(owner_user_id=owner, draft_kind="route",
        target_id=rid, base_version_id=None, initial_content=payload)
    return rid, draft

rid, original = route()
service.confirm_draft(original["draft_id"], actor_id=owner, revision=1, idempotency_key=uuid.uuid4().hex)
other, duplicate = route()
cid = "crs_" + uuid.uuid4().hex[:12]
with db.session() as sess:
    single_title = "合成独立 Python" + (" 大图" if len(sys.argv) > 2 and sys.argv[2] == "large" else "")
    sess.add(CourseRow(course_id=cid, owner_user_id=owner, title_ref=single_title))
single = service.find_or_create_draft(owner_user_id=owner, draft_kind="course_outline",
    target_id=cid, base_version_id=None, initial_content={"units": [{"title": "语法", "lessons": [
        {"title": "变量", "goal": "理解变量"}]}]})
service.confirm_draft(single["draft_id"], actor_id=owner, revision=1, idempotency_key=uuid.uuid4().hex)
task_id = "task_" + uuid.uuid4().hex[:12]
with db.session() as sess:
    sess.add(PlanningTaskStateRow(task_id=task_id, owner_user_id=owner, planning_scope="route",
        resolved_scope="route", target_kind="route", target_id=other, draft_id=duplicate["draft_id"]))
print(json.dumps({"route_id": rid, "duplicate_route_id": other, "draft_id": duplicate["draft_id"],
                  "task_id": task_id, "independent_course_id": cid}))

"""Single synthetic course for the browser learning/assessment flow, isolated schema only."""
import sys,json,uuid
from sqlalchemy import text
from app.logging_audit.db import Database
from app.planning.models import CourseRow
from app.planning.drafts import DraftService

db=Database()
with db.engine.connect() as c:assert c.execute(text("select current_schema()")).scalar().startswith("course_library_validation_")
course_id="crs_"+uuid.uuid4().hex[:12];owner=sys.argv[1]
with db.session() as sess:sess.add(CourseRow(course_id=course_id,owner_user_id=owner,title_ref="字典与键值查找"))
service=DraftService(db)
item=service.find_or_create_draft(owner_user_id=owner,draft_kind="course_outline",target_id=course_id,
    base_version_id=None,initial_content={"title":"字典与键值查找","units":[{"title":"字典基础","lessons":[
        {"title":"键值查找","goal":"能通过键查找值"}]}],"prerequisites":[]})
service.confirm_draft(item["draft_id"],actor_id=owner,revision=1,idempotency_key=uuid.uuid4().hex)
print(json.dumps({"course_id":course_id}))

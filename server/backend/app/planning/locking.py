"""Confirmed structure is immutable at every write boundary (ADR-029)."""
from sqlalchemy import select
from .models import LearningRouteRow, CourseRow, PlanningTaskStateRow
from .errors import error


def planning_target(sess, kind, ident, owner, *, mutable=False, allow_abandoned=False):
    target = sess.get(LearningRouteRow if kind in ("route", "route_generate") else CourseRow,
                      ident, with_for_update=mutable)
    if target is None or target.owner_user_id != owner or target.deleted_at:
        raise error("draft_not_found", status=404)
    if not allow_abandoned and sess.scalar(select(PlanningTaskStateRow.task_id).where(
        PlanningTaskStateRow.target_id == ident, PlanningTaskStateRow.owner_user_id == owner,
        PlanningTaskStateRow.abandoned_at.is_not(None), PlanningTaskStateRow.draft_id.is_not(None)).limit(1)):
        raise error("TASK_CANCELLED", "这次规划已放弃。", status=409)
    locked = bool(target.current_route_version_id if isinstance(target, LearningRouteRow)
                  else target.current_outline_version_id)
    if mutable and locked:
        raise error("PLANNING_LOCKED", "课程已确认，不能再调整规划。", status=409)
    return target, locked

"""One cover per visible planning root, matching library grouping exactly."""
from sqlalchemy import exists, select
from .errors import error
from .models import CourseRow, LearningRouteRow, RouteCourseNodeRow, RouteDetachedCourseRow, RouteVersionRow


def projection(kind, ident, parent_id=None):
    return {"cover_eligible": parent_id is None,
        "cover_owner_kind": "route" if parent_id else kind,
        "cover_owner_id": parent_id or ident}


def grouped_owners(routes, versions, nodes, detached):
    """Historic membership counts; an explicit detach removes that route's membership."""
    owners = {}
    for route in sorted(routes.values(), key=lambda row: row.route_id):
        if route.current_route_version_id not in versions:
            continue
        for version in versions.values():
            if version.route_id != route.route_id:
                continue
            for node in nodes[version.route_version_id]:
                if (route.route_id, node.course_id) not in detached:
                    owners.setdefault(node.course_id, route.route_id)
    return owners


def resolve(sess, kind, ident, owner):
    model = {"route": LearningRouteRow, "course": CourseRow}.get(kind)
    target = sess.get(model, ident) if model else None
    if not target or target.owner_user_id != owner or target.deleted_at:
        raise error("draft_not_found", status=404)
    if kind == "route":
        return projection(kind, ident)
    active_versions = select(RouteVersionRow.route_version_id).join(LearningRouteRow,
        RouteVersionRow.route_id == LearningRouteRow.route_id).where(
        LearningRouteRow.owner_user_id == owner, LearningRouteRow.deleted_at.is_(None)).correlate(None)
    detached = exists(select(RouteDetachedCourseRow.course_id).where(
        RouteDetachedCourseRow.route_id == LearningRouteRow.route_id,
        RouteDetachedCourseRow.course_id == ident))
    parent = sess.scalar(select(LearningRouteRow.route_id).join(RouteVersionRow,
        RouteVersionRow.route_id == LearningRouteRow.route_id).join(RouteCourseNodeRow,
        RouteCourseNodeRow.route_version_id == RouteVersionRow.route_version_id).where(
        LearningRouteRow.owner_user_id == owner, LearningRouteRow.deleted_at.is_(None),
        LearningRouteRow.current_route_version_id.in_(active_versions),
        RouteCourseNodeRow.course_id == ident, ~detached)
        .order_by(LearningRouteRow.route_id).limit(1))
    return projection(kind, ident, parent)


def suppressed(policy):
    return {**policy, "status": "suppressed", "asset_id": None, "task_id": None,
        "image_url": None, "method": None, "requested_method": None, "actual_method": None,
        "fallback_reason": None, "error_code": None, "retryable": False}


def require_eligible(sess, kind, ident, owner):
    policy = resolve(sess, kind, ident, owner)
    if not policy["cover_eligible"]:
        raise error("TASK_CANCELLED", "cover belongs to the planning root", status=409)
    return policy

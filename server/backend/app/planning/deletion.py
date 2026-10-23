"""路线/课程删除流（切片 5）。

- 删除路线连同当前编排中的成员课程一并删除；历史版本成员与已移出的课程保留；
- 移出路线仍保留课程（课程继续可学；路线引用只留已删除提示）；
- 删除课程清理草稿、版本、讲次与教学产物；教学会话软删；
- 显式删除即写墓碑（独立删除记录源），恢复流程归 RestoreService；
- 全部写审计（resource.delete.requested）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete as sa_delete, select

from ..logging_audit.db import Database
from ..logging_audit.repositories import PgAuditLog
from ..materials.models import DeletionTombstoneRow
from ..tasks.service import LearningTaskRow
from .errors import error
from .models import (
    ConversationRow,
    CourseRow,
    CourseUnitRow,
    LearningRouteRow,
    LessonPrerequisiteEdgeRow,
    LessonRow,
    OutlineVersionRow,
    PlanningCandidateRow,
    PlanningDraftRow,
    RouteCourseNodeRow,
    RoutePrerequisiteEdgeRow,
    RouteStageRow,
    RouteVersionRow,
    RouteDetachedCourseRow,
    utcnow,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tombstone(sess, *, object_type: str, object_id: str, owner: str) -> str:
    tombstone_id = f"tmb_{uuid.uuid4().hex[:12]}"
    sess.add(
        DeletionTombstoneRow(
            tombstone_id=tombstone_id,
            object_type=object_type,
            object_id=object_id,
            owner_user_id=owner,
            deleted_at=_now(),
            cleanup_stage="pending",
        )
    )
    return tombstone_id


def _cancel_queued(sess, *, owner: str, subject_ids: list[str]) -> int:
    if not subject_ids:
        return 0
    tasks = (
        sess.execute(
            select(LearningTaskRow).where(
                LearningTaskRow.owner_user_id == owner,
                LearningTaskRow.subject_id.in_(subject_ids),
                LearningTaskRow.status.in_(("queued", "leased")),
            )
        )
        .scalars()
        .all()
    )
    for t in tasks:
        t.cancel_requested = True
        if t.status == "queued":
            t.status = "cancelled"
    return len(tasks)


def _audit(audit: Optional[PgAuditLog], *, actor_id: str, object_type: str,
           object_id: str, result: str) -> None:
    if audit is None:
        return
    try:
        audit.append(
            "resource.delete.requested", actor_id=actor_id,
            object_type=object_type, object_id=object_id, result=result,
        )
    except Exception:  # noqa: BLE001 - 审计失败必须可见
        raise


def delete_feature_data(sess, kind, target_id):
    from sqlalchemy import update
    from .models import PlanningTaskStateRow
    sess.execute(update(PlanningTaskStateRow).where(
        PlanningTaskStateRow.target_kind == kind, PlanningTaskStateRow.target_id == target_id
    ).values(process_json="{}"))
    from .feature_models import PlanningDialogueRow, CoverAssetRow, CoverStateRow
    if kind == "course":
        from ..review.models import ReviewAnswerRow, ReviewCardRow, ReviewLogRow, ReviewSessionRow
        review_sessions = select(ReviewSessionRow.session_id).where(ReviewSessionRow.course_id == target_id)
        review_answers = select(ReviewAnswerRow.answer_id).where(ReviewAnswerRow.session_id.in_(review_sessions))
        sess.execute(sa_delete(ReviewLogRow).where(ReviewLogRow.answer_id.in_(review_answers)))
        sess.execute(sa_delete(ReviewAnswerRow).where(ReviewAnswerRow.session_id.in_(review_sessions)))
        sess.execute(sa_delete(ReviewSessionRow).where(ReviewSessionRow.course_id == target_id))
        sess.execute(sa_delete(ReviewCardRow).where(ReviewCardRow.course_id == target_id))
        from ..teaching.unit_scope import UnitScopeRow
        sess.execute(sa_delete(UnitScopeRow).where(UnitScopeRow.course_id == target_id))
        from ..teaching.learning_models import LessonBlueprintRow, UnitAssessmentRow, AssessmentAttemptRow, PreparedLessonPlanRow, TeachingPreviewRow
        assessments = select(UnitAssessmentRow.assessment_id).where(UnitAssessmentRow.course_id == target_id)
        sess.execute(sa_delete(AssessmentAttemptRow).where(AssessmentAttemptRow.assessment_id.in_(assessments)))
        sess.execute(sa_delete(UnitAssessmentRow).where(UnitAssessmentRow.course_id == target_id))
        conversations = select(ConversationRow.conversation_id).where(ConversationRow.course_id == target_id)
        from ..teaching.learning_models import (FeynmanSessionRow, TaughtKnowledgeRow,
            SegmentCoverageRow, ChannelPresentationRow)
        sess.execute(sa_delete(TaughtKnowledgeRow).where(TaughtKnowledgeRow.course_id == target_id))
        sess.execute(sa_delete(SegmentCoverageRow).where(SegmentCoverageRow.conversation_id.in_(conversations)))
        sess.execute(sa_delete(ChannelPresentationRow).where(ChannelPresentationRow.conversation_id.in_(conversations)))
        sess.execute(sa_delete(FeynmanSessionRow).where(FeynmanSessionRow.conversation_id.in_(conversations)))
        sess.execute(sa_delete(LessonBlueprintRow).where(LessonBlueprintRow.conversation_id.in_(conversations)))
        sess.execute(sa_delete(TeachingPreviewRow).where(TeachingPreviewRow.conversation_id.in_(conversations)))
        sess.execute(sa_delete(PreparedLessonPlanRow).where(PreparedLessonPlanRow.course_id == target_id))
    draft_ids = select(PlanningDraftRow.draft_id).where(PlanningDraftRow.target_id == target_id)
    sess.execute(sa_delete(PlanningDialogueRow).where(PlanningDialogueRow.draft_id.in_(draft_ids)))
    sess.execute(sa_delete(CoverAssetRow).where(CoverAssetRow.target_kind == kind, CoverAssetRow.target_id == target_id))
    sess.execute(sa_delete(CoverStateRow).where(CoverStateRow.target_kind == kind, CoverStateRow.target_id == target_id))


def _current_member_course_ids(sess, route, actor_id: str) -> list:
    """当前路线版本编排中的成员课程（排除已显式移出的课程与已删除课程）。"""
    if not route.current_route_version_id:
        return []
    detached = select(RouteDetachedCourseRow.course_id).where(
        RouteDetachedCourseRow.route_id == route.route_id)
    return list(
        sess.execute(
            select(CourseRow)
            .join(RouteCourseNodeRow, RouteCourseNodeRow.course_id == CourseRow.course_id)
            .where(
                RouteCourseNodeRow.route_version_id == route.current_route_version_id,
                CourseRow.owner_user_id == actor_id,
                CourseRow.deleted_at.is_(None),
                ~CourseRow.course_id.in_(detached),
            )
            .distinct()
        )
        .scalars()
        .all()
    )


def _purge_course(sess, course_id: str, actor_id: str) -> list[str]:
    """硬删课程派生产物（大纲版本/单元/讲次/先修边/草稿/候选），软删教学会话并写墓碑。

    课程行本身的软删与课程墓碑由调用方负责；返回被软删的教学会话 id 列表。
    """
    from ..memory.service import delete_course_memory
    delete_course_memory(sess, actor_id, course_id)
    delete_feature_data(sess, "course", course_id)
    outline_ids = [
        r for r in sess.execute(
            select(OutlineVersionRow.outline_version_id)
            .where(OutlineVersionRow.course_id == course_id)
        ).scalars().all()
    ]
    from .models import LessonContentVersionRow, LessonProgressRow
    for ov_id in outline_ids:
        lesson_ids = select(LessonRow.lesson_id).where(LessonRow.outline_version_id == ov_id)
        sess.execute(sa_delete(LessonProgressRow).where(LessonProgressRow.lesson_id.in_(lesson_ids)))
        sess.execute(sa_delete(LessonContentVersionRow).where(LessonContentVersionRow.lesson_id.in_(lesson_ids)))
        # 派生产物：讲次、单元、先修边、版本（硬删；课程墓碑已记录删除事实）
        sess.execute(sa_delete(LessonRow).where(LessonRow.outline_version_id == ov_id))
        sess.execute(sa_delete(CourseUnitRow).where(CourseUnitRow.outline_version_id == ov_id))
        sess.execute(
            sa_delete(LessonPrerequisiteEdgeRow)
            .where(LessonPrerequisiteEdgeRow.outline_version_id == ov_id)
        )
        sess.execute(sa_delete(OutlineVersionRow).where(OutlineVersionRow.outline_version_id == ov_id))
    sess.execute(sa_delete(PlanningDraftRow).where(PlanningDraftRow.target_id == course_id))
    sess.execute(sa_delete(PlanningCandidateRow).where(PlanningCandidateRow.target_id == course_id))
    # 教学会话软删 + 墓碑
    convs = (
        sess.execute(
            select(ConversationRow).where(
                ConversationRow.course_id == course_id,
                ConversationRow.deleted_at.is_(None),
            )
        )
        .scalars()
        .all()
    )
    conv_ids = []
    for conv in convs:
        conv.deleted_at = _now()
        conv_ids.append(conv.conversation_id)
        _tombstone(sess, object_type="conversation", object_id=conv.conversation_id, owner=actor_id)
    return conv_ids


class PlanningDeletionService:
    def __init__(self, db: Database, audit: Optional[PgAuditLog] = None) -> None:
        self.db = db
        self.audit = audit

    def detach_course(self, route_id: str, course_id: str, *, actor_id: str) -> dict:
        with self.db.session() as sess:
            route = sess.get(LearningRouteRow, route_id, with_for_update=True)
            course = sess.get(CourseRow, course_id)
            if (route is None or course is None or route.owner_user_id != actor_id
                or course.owner_user_id != actor_id or route.deleted_at or course.deleted_at):
                raise error("course_not_found", status=404)
            from .locking import planning_target
            planning_target(sess, "route", route_id, actor_id, mutable=True)
            member = sess.execute(select(RouteCourseNodeRow).where(
                RouteCourseNodeRow.route_version_id == route.current_route_version_id,
                RouteCourseNodeRow.course_id == course_id)).scalars().first()
            if member is None:
                raise error("course_not_found", status=404)
            if sess.get(RouteDetachedCourseRow, (route_id, course_id)) is None:
                sess.add(RouteDetachedCourseRow(route_id=route_id, course_id=course_id))
            if self.audit:
                self.audit.append("config.changed", actor_id=actor_id, object_type="course",
                                  object_id=course_id, result="route_detached")
        return {"route_id": route_id, "course_id": course_id, "detached": True, "course_kept": True}

    def delete_route(self, route_id: str, *, actor_id: str) -> dict:
        """删除路线：连同当前编排中的成员课程一并删除。

        仅删除当前路线版本编排中的成员课程；历史版本成员与已显式移出的课程保留
        （共享资料始终不动）。返回体给出被级联删除的课程 id，便于调用方提示。
        """
        with self.db.session() as sess:
            route = sess.get(LearningRouteRow, route_id, with_for_update=True)
            if route is None or route.owner_user_id != actor_id or route.deleted_at is not None:
                raise error("route_not_found", status=404)
            member_ids = _current_member_course_ids(sess, route, actor_id)
            delete_feature_data(sess, "route", route_id)
            route.deleted_at = _now()
            tombstone_id = _tombstone(sess, object_type="route", object_id=route_id, owner=actor_id)
            course_ids = []
            for course in member_ids:
                course.deleted_at = _now()
                _tombstone(sess, object_type="course", object_id=course.course_id, owner=actor_id)
                _purge_course(sess, course.course_id, actor_id)
                course_ids.append(course.course_id)
            _cancel_queued(sess, owner=actor_id, subject_ids=[route_id, *course_ids])
            sess.commit()
        _audit(self.audit, actor_id=actor_id, object_type="route",
               object_id=route_id, result=f"tombstone={tombstone_id}")
        for course_id in course_ids:
            _audit(self.audit, actor_id=actor_id, object_type="course",
                   object_id=course_id, result=f"cascade_of_route={route_id}")
        return {"route_id": route_id, "deleted": True, "courses_deleted": len(course_ids),
                "course_ids": course_ids, "tombstone_id": tombstone_id}

    def delete_course(self, course_id: str, *, actor_id: str) -> dict:
        """删除课程：清理草稿、版本、讲次与教学产物；教学会话软删。"""
        with self.db.session() as sess:
            course = sess.get(CourseRow, course_id)
            if course is None or course.owner_user_id != actor_id or course.deleted_at is not None:
                raise error("course_not_found", status=404)
            course.deleted_at = _now()
            tombstone_id = _tombstone(sess, object_type="course", object_id=course_id, owner=actor_id)
            conv_ids = _purge_course(sess, course_id, actor_id)
            _cancel_queued(sess, owner=actor_id, subject_ids=[course_id, *conv_ids])
            sess.commit()
        _audit(self.audit, actor_id=actor_id, object_type="course",
               object_id=course_id, result=f"tombstone={tombstone_id}")
        return {"course_id": course_id, "deleted": True,
                "teaching_conversations_deleted": len(conv_ids),
                "tombstone_id": tombstone_id}


__all__ = ["PlanningDeletionService"]

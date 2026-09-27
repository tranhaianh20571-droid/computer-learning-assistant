"""路线/课程删除流（切片 5）。

- 移出或删除路线保留课程（课程继续可学；路线引用只留已删除提示）；
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


class PlanningDeletionService:
    def __init__(self, db: Database, audit: Optional[PgAuditLog] = None) -> None:
        self.db = db
        self.audit = audit

    def delete_route(self, route_id: str, *, actor_id: str) -> dict:
        """删除路线：课程保留，只解除编排。"""
        with self.db.session() as sess:
            route = sess.get(LearningRouteRow, route_id)
            if route is None or route.owner_user_id != actor_id or route.deleted_at is not None:
                raise error("route_not_found", status=404)
            route.deleted_at = _now()
            tombstone_id = _tombstone(sess, object_type="route", object_id=route_id, owner=actor_id)
            # 课程保留：来源引用保持，但版本不可再展开新大纲
            _cancel_queued(sess, owner=actor_id, subject_ids=[route_id])
            sess.commit()
        _audit(self.audit, actor_id=actor_id, object_type="route",
               object_id=route_id, result=f"tombstone={tombstone_id}")
        return {"route_id": route_id, "deleted": True, "courses_kept": True,
                "tombstone_id": tombstone_id}

    def delete_course(self, course_id: str, *, actor_id: str) -> dict:
        """删除课程：清理草稿、版本、讲次与教学产物；教学会话软删。"""
        with self.db.session() as sess:
            course = sess.get(CourseRow, course_id)
            if course is None or course.owner_user_id != actor_id or course.deleted_at is not None:
                raise error("course_not_found", status=404)
            course.deleted_at = _now()
            tombstone_id = _tombstone(sess, object_type="course", object_id=course_id, owner=actor_id)

            outline_ids = [
                r for r in sess.execute(
                    select(OutlineVersionRow.outline_version_id)
                    .where(OutlineVersionRow.course_id == course_id)
                ).scalars().all()
            ]
            for ov_id in outline_ids:
                # 派生产物：讲次、单元、先修边、版本（硬删；课程墓碑已记录删除事实）
                sess.execute(
                    sa_delete(LessonRow).where(LessonRow.outline_version_id == ov_id)
                )
                sess.execute(
                    sa_delete(CourseUnitRow).where(CourseUnitRow.outline_version_id == ov_id)
                )
                sess.execute(
                    sa_delete(LessonPrerequisiteEdgeRow)
                    .where(LessonPrerequisiteEdgeRow.outline_version_id == ov_id)
                )
                sess.execute(
                    sa_delete(OutlineVersionRow).where(OutlineVersionRow.outline_version_id == ov_id)
                )
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
                _tombstone(sess, object_type="conversation", object_id=conv.conversation_id,
                           owner=actor_id)
            _cancel_queued(sess, owner=actor_id, subject_ids=[course_id, *conv_ids])
            sess.commit()
        _audit(self.audit, actor_id=actor_id, object_type="course",
               object_id=course_id, result=f"tombstone={tombstone_id}")
        return {"course_id": course_id, "deleted": True,
                "teaching_conversations_deleted": len(conv_ids),
                "tombstone_id": tombstone_id}


__all__ = ["PlanningDeletionService"]

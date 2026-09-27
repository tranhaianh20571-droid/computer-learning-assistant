"""路线总览与课程总览查询（T07）。

- 总览与导图从同一个已确认版本读取（同版口径）；
- 课程进度按当前已确认大纲的讲次等权计算；lesson_progress 本切片只有
  「未开始」默认值（完成写入归切片 4）；
- 路线总览不输出整体百分比（§8.1 明确不做）；
- 未确认大纲（current_outline_version_id 为空）的课程返回受控门槛状态，
  前端据此禁用「开始学习」入口；服务端教学门槛另有 LESSON_UNLOCK_REQUIRED。
"""

from __future__ import annotations

from sqlalchemy import select

from ..logging_audit.db import Database
from .errors import error
from .models import (
    CourseRow,
    CourseUnitRow,
    LearningNavigationStateRow,
    LessonProgressRow,
    LessonPrerequisiteEdgeRow,
    LessonRow,
    OutlineVersionRow,
    LearningRouteRow,
    RouteCourseNodeRow,
    RoutePrerequisiteEdgeRow,
    RouteStageRow,
    RouteVersionRow,
    utcnow,
)

# 「安排过先修 ≠ 已掌握」提示的稳定键（前端按切片 0 视觉规则渲染）
PREREQ_NOT_MASTERY_HINT = "prereq_arranged_not_mastered"


class OverviewService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------
    def route_overview(self, route_id: str, *, actor_id: str) -> dict:
        with self.db.session() as sess:
            route = sess.get(LearningRouteRow, route_id)
            if route is None or route.owner_user_id != actor_id or route.deleted_at is not None:
                raise error("route_not_found", status=404)
            version = (
                sess.get(RouteVersionRow, route.current_route_version_id)
                if route.current_route_version_id
                else None
            )
            stages: list[dict] = []
            if version is not None:
                stage_rows = (
                    sess.execute(
                        select(RouteStageRow)
                        .where(RouteStageRow.route_version_id == version.route_version_id)
                        .order_by(RouteStageRow.order_index)
                    )
                    .scalars()
                    .all()
                )
                node_rows = (
                    sess.execute(
                        select(RouteCourseNodeRow).where(
                            RouteCourseNodeRow.route_version_id == version.route_version_id
                        )
                    )
                    .scalars()
                    .all()
                )
                nodes_by_stage: dict[str, list[RouteCourseNodeRow]] = {}
                for node in node_rows:
                    nodes_by_stage.setdefault(node.stage_id, []).append(node)
                for stage in stage_rows:
                    courses = []
                    for node in sorted(nodes_by_stage.get(stage.stage_id, []), key=lambda n: n.order_index):
                        course = sess.get(CourseRow, node.course_id)
                        courses.append(
                            {
                                "course_id": node.course_id,
                                "node_id": node.node_id,
                                "order_index": node.order_index,
                                "title": course.title_ref if course else "",
                                "goal_snapshot": node.goal_snapshot_ref,
                                "outline_confirmed": bool(course and course.current_outline_version_id),
                                "progress": self.course_progress(sess, node.course_id),
                            }
                        )
                    stages.append(
                        {
                            "stage_id": stage.stage_id,
                            "order_index": stage.order_index,
                            "title": stage.title_ref,
                            "course_count": len(courses),
                            "courses": courses,
                        }
                    )
            return {
                "route_id": route.route_id,
                "scenario": route.scenario,
                "current_version_id": version.route_version_id if version else None,
                "version_no": version.version_no if version else None,
                "has_version": version is not None,
                "stages": stages,
                # 注意：不输出路线整体百分比（§8.1）
            }

    # ------------------------------------------------------------------
    def course_overview(self, course_id: str, *, actor_id: str) -> dict:
        with self.db.session() as sess:
            course = sess.get(CourseRow, course_id)
            if course is None or course.owner_user_id != actor_id or course.deleted_at is not None:
                raise error("course_not_found", status=404)
            version = (
                sess.get(OutlineVersionRow, course.current_outline_version_id)
                if course.current_outline_version_id
                else None
            )
            units: list[dict] = []
            lesson_total = 0
            if version is not None:
                unit_rows = (
                    sess.execute(
                        select(CourseUnitRow)
                        .where(CourseUnitRow.outline_version_id == version.outline_version_id)
                        .order_by(CourseUnitRow.order_index)
                    )
                    .scalars()
                    .all()
                )
                lesson_rows = (
                    sess.execute(
                        select(LessonRow).where(
                            LessonRow.outline_version_id == version.outline_version_id
                        )
                    )
                    .scalars()
                    .all()
                )
                lessons_by_unit: dict[str, list[LessonRow]] = {}
                for lesson in lesson_rows:
                    lessons_by_unit.setdefault(lesson.unit_id, []).append(lesson)
                prereq_targets = set(
                    sess.execute(
                        select(LessonPrerequisiteEdgeRow.to_lesson_id).where(
                            LessonPrerequisiteEdgeRow.outline_version_id == version.outline_version_id
                        )
                    ).scalars().all()
                )
                for unit in unit_rows:
                    lessons = []
                    for lesson in sorted(lessons_by_unit.get(unit.unit_id, []), key=lambda l: l.order_index):
                        lesson_total += 1
                        progress = sess.execute(
                            select(LessonProgressRow).where(
                                LessonProgressRow.owner_user_id == actor_id,
                                LessonProgressRow.lesson_id == lesson.lesson_id,
                            )
                        ).scalar_one_or_none()
                        lessons.append(
                            {
                                "lesson_id": lesson.lesson_id,
                                "order_index": lesson.order_index,
                                "title": lesson.goal_ref or "",
                                "goal": lesson.goal_ref,
                                "status_preparation": lesson.status_preparation,
                                "learning_state": progress.learning_state if progress else "not_started",
                                "prereq_arranged": lesson.lesson_id in prereq_targets,
                                "prereq_hint": PREREQ_NOT_MASTERY_HINT
                                if lesson.lesson_id in prereq_targets
                                else None,
                                # 教学门槛：大纲已确认但讲次未就绪 → 入口禁用原因
                                "startable": lesson.status_preparation == "ready",
                            }
                        )
                    units.append(
                        {
                            "unit_id": unit.unit_id,
                            "order_index": unit.order_index,
                            "title": unit.title_ref,
                            "lesson_count": len(lessons),
                            "lessons": lessons,
                        }
                    )
            return {
                "course_id": course.course_id,
                "scenario": course.scenario,
                "current_version_id": version.outline_version_id if version else None,
                "version_no": version.version_no if version else None,
                "outline_confirmed": version is not None,
                # 教学门槛：未确认大纲 → 服务端禁止教学/TTS（切片 4 阈值的前置校验）
                "teaching_unlocked": version is not None,
                "source_route_version_id": course.source_route_version_id,
                "source_route_node_id": course.source_route_node_id,
                "units": units,
                "progress": {
                    "lesson_total": lesson_total,
                    # 本切片 lesson_progress 全为 not_started → started=0（完成写入归切片 4）
                    "lesson_started": self._started_count(sess, actor_id, course.course_id, version),
                },
            }

    def _started_count(self, sess, actor_id: str, course_id: str, version) -> int:
        if version is None:
            return 0
        lesson_ids = set(
            sess.execute(
                select(LessonRow.lesson_id).where(
                    LessonRow.outline_version_id == version.outline_version_id
                )
            ).scalars().all()
        )
        if not lesson_ids:
            return 0
        rows = (
            sess.execute(
                select(LessonProgressRow).where(
                    LessonProgressRow.owner_user_id == actor_id,
                    LessonProgressRow.lesson_id.in_(list(lesson_ids)),
                    LessonProgressRow.learning_state != "not_started",
                )
            )
            .scalars()
            .all()
        )
        return len(rows)

    def course_progress(self, sess, course_id: str) -> dict:
        course = sess.get(CourseRow, course_id)
        if course is None or not course.current_outline_version_id:
            return {"lesson_total": 0, "lesson_started": 0, "outline_confirmed": False}
        version = sess.get(OutlineVersionRow, course.current_outline_version_id)
        total = len(
            sess.execute(
                select(LessonRow.lesson_id).where(
                    LessonRow.outline_version_id == version.outline_version_id
                )
            ).scalars().all()
        )
        return {"lesson_total": total, "lesson_started": 0, "outline_confirmed": True}

    # ------------------------------------------------------------------
    def update_navigation(
        self,
        *,
        actor_id: str,
        route_id: str | None = None,
        course_id: str | None = None,
        unit_id: str | None = None,
        lesson_id: str | None = None,
    ) -> dict:
        with self.db.session() as sess:
            row = sess.get(LearningNavigationStateRow, actor_id)
            if row is None:
                row = LearningNavigationStateRow(owner_user_id=actor_id)
                sess.add(row)
            if route_id is not None:
                row.last_route_id = route_id
            if course_id is not None:
                row.last_course_id = course_id
            if unit_id is not None:
                row.last_unit_id = unit_id
            if lesson_id is not None:
                row.last_lesson_id = lesson_id
            row.updated_at = utcnow()
            sess.commit()
            return {
                "last_route_id": row.last_route_id,
                "last_course_id": row.last_course_id,
                "last_unit_id": row.last_unit_id,
                "last_lesson_id": row.last_lesson_id,
            }

    def get_navigation(self, *, actor_id: str) -> dict:
        with self.db.session() as sess:
            row = sess.get(LearningNavigationStateRow, actor_id)
            if row is None:
                return {"last_route_id": None, "last_course_id": None,
                        "last_unit_id": None, "last_lesson_id": None}
            return {
                "last_route_id": row.last_route_id,
                "last_course_id": row.last_course_id,
                "last_unit_id": row.last_unit_id,
                "last_lesson_id": row.last_lesson_id,
            }


__all__ = ["OverviewService", "PREREQ_NOT_MASTERY_HINT"]

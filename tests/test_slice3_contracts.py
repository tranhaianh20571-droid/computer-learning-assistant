"""切片 3 T03 契约测试：错误码、输出模型、DAG/结构校验、场景白名单与迁移一致性。"""

from __future__ import annotations

import pytest

from common_fixtures import (  # noqa: F401
    OUTLINE_OK_PAYLOAD,
    ROUTE_OK_PAYLOAD,
    ROOT,
    capability_status,
)

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.contracts import ERROR_CODES, validate_error_code  # noqa: E402
from backend.planning.errors import error  # noqa: E402
from backend.planning.outputs import (  # noqa: E402
    CONTEXT_BUDGET_DEFAULTS,
    OutlineCandidate,
    RouteCandidate,
    SCENE_ELEMENT_WHITELIST,
    validate_content_json,
    validate_outline_candidate,
    validate_route_candidate,
)
from backend.planning.scene import (  # noqa: E402
    LAYOUT,
    measure_text_width,
    outline_scene,
    route_scene,
    validate_scene,
    wrap_label,
)

# 已确认版本形态（带业务 ID）——场景转换层的合法输入
ROUTE_VERSION_PAYLOAD = {
    "stages": [
        {"id": "stg_a", "title": "阶段一：基础（超长标题观察标点换行与截断策略）", "courses": [
            {"id": "crs_a1", "title": "数据库基础：关系模型与 SQL（Mixed Terms 混排）"},
            {"id": "crs_a2", "title": "SQL 实践"},
        ]},
        {"id": "stg_b", "title": "阶段二", "courses": [
            {"id": "crs_b1", "title": "事务与并发"},
            {"id": "crs_b2", "title": "索引与优化"},
        ]},
    ],
    "prerequisites": [{"from": "crs_a1", "to": "crs_b1"}],
}

OUTLINE_VERSION_PAYLOAD = {
    "units": [
        {"id": "unit_a", "title": "单元一（长单元标题观察换行）", "lessons": [
            {"id": "les_a1", "title": "第 1 讲"},
            {"id": "les_a2", "title": "事务隔离级别与并发异常：脏读、不可重复读、幻读的判定（长中文讲次标题）"},
        ]},
        {"id": "unit_b", "title": "单元二", "lessons": [
            {"id": "les_b1", "title": "第 3 讲"},
            {"id": "les_b2", "title": "第 4 讲"},
        ]},
    ],
    "prerequisites": [{"from": "les_a1", "to": "les_a2"}],
}


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db

NEW_ERROR_CODES = {
    "DRAFT_CONFLICT": (False, True),
    "DRAFT_REVISION_MISMATCH": (False, True),
    "INVALID_OUTLINE_STRUCTURE": (False, True),
    "DAG_CYCLE": (False, True),
    "INVALID_PREREQUISITE": (False, True),
    "ROUTE_STRUCTURE_INVALID": (False, True),
    "DUPLICATE_IDEMPOTENCY_KEY": (False, True),
    "AWAITING_SCOPE_CLARIFICATION": (False, True),
    "AWAITING_SEARCH_DECISION": (False, True),
    "MODEL_CAPABILITY_UNAVAILABLE": (False, True),
    "CONTEXT_BUDGET_EXCEEDED": (False, True),
    "LESSON_UNLOCK_REQUIRED": (False, True),
}


class TestErrorCodes:
    @pytest.mark.parametrize("code", sorted(NEW_ERROR_CODES))
    def test_registered(self, code: str) -> None:
        spec = validate_error_code(code)
        retryable, user_visible = NEW_ERROR_CODES[code]
        assert spec.retryable is retryable
        assert spec.user_visible is user_visible

    def test_wait_states_not_failures(self) -> None:
        # 等待态不可标记为可重试失败（不计为失败语义）
        assert ERROR_CODES["AWAITING_SEARCH_DECISION"].alert_level.value == "none"
        assert ERROR_CODES["AWAITING_SCOPE_CLARIFICATION"].alert_level.value == "none"


class TestOutputModels:
    def test_route_candidate_ok(self) -> None:
        candidate = RouteCandidate.model_validate(ROUTE_OK_PAYLOAD)
        summary = validate_route_candidate(candidate)
        assert summary == {"stage_count": 2, "course_count": 4}

    def test_route_empty_stage_rejected(self) -> None:
        payload = {"stages": [{"title": "s", "courses": []}]}
        with pytest.raises(Exception) as exc:
            validate_content_json("route", payload)
        assert exc.value.code == "ROUTE_STRUCTURE_INVALID"

    def test_route_needs_at_least_one_course(self) -> None:
        with pytest.raises(Exception) as exc:
            validate_content_json("route", {"stages": []})
        assert exc.value.code in ("ROUTE_STRUCTURE_INVALID",)

    def test_route_prereq_index_out_of_range(self) -> None:
        payload = {
            "stages": [{"title": "s", "courses": [{"title": "c"}]}],
            "prerequisites": [{"from_index": 0, "to_index": 5}],
        }
        with pytest.raises(Exception) as exc:
            validate_content_json("route", payload)
        assert exc.value.code == "INVALID_PREREQUISITE"

    def test_route_dag_cycle_rejected(self) -> None:
        payload = {
            "stages": [{"title": "s", "courses": [{"title": "a"}, {"title": "b"}]}],
            "prerequisites": [{"from_index": 0, "to_index": 1}, {"from_index": 1, "to_index": 0}],
        }
        with pytest.raises(Exception) as exc:
            validate_content_json("route", payload)
        assert exc.value.code == "DAG_CYCLE"

    def test_outline_ok(self) -> None:
        candidate = OutlineCandidate.model_validate(OUTLINE_OK_PAYLOAD)
        summary = validate_outline_candidate(candidate)
        assert summary == {"unit_count": 2, "lesson_count": 4}

    def test_outline_empty_unit_rejected(self) -> None:
        payload = {"units": [{"title": "u", "lessons": []}]}
        with pytest.raises(Exception) as exc:
            validate_content_json("course_outline", payload)
        assert exc.value.code == "INVALID_OUTLINE_STRUCTURE"

    def test_outline_dag_cycle_rejected(self) -> None:
        payload = {
            "units": [{"title": "u", "lessons": [{"title": "a"}, {"title": "b"}]}],
            "prerequisites": [{"from_index": 1, "to_index": 0}, {"from_index": 0, "to_index": 1}],
        }
        with pytest.raises(Exception) as exc:
            validate_content_json("course_outline", payload)
        assert exc.value.code == "DAG_CYCLE"

    def test_text_length_limits(self) -> None:
        too_long = "超" * 200
        payload = {"stages": [{"title": "s", "courses": [{"title": too_long}]}]}
        with pytest.raises(Exception):
            RouteCandidate.model_validate(payload)

    def test_budget_defaults_frozen(self) -> None:
        assert set(CONTEXT_BUDGET_DEFAULTS) == {
            "max_input_tokens", "max_retrieval_chars", "max_retrieval_chunks",
            "max_images", "max_history_messages",
        }
        assert CONTEXT_BUDGET_DEFAULTS["max_input_tokens"] > 0


class TestSceneContract:
    def test_whitelist_frozen(self) -> None:
        assert set(SCENE_ELEMENT_WHITELIST) == {"rectangle", "ellipse", "text", "arrow", "line"}

    def test_route_scene_deterministic_and_whitelisted(self) -> None:
        scene1 = route_scene(ROUTE_VERSION_PAYLOAD)
        scene2 = route_scene(ROUTE_VERSION_PAYLOAD)
        assert scene1 == scene2  # 确定性：同输入同输出
        for el in scene1["elements"]:
            assert el["type"] in SCENE_ELEMENT_WHITELIST
        validate_scene(scene1)

    def test_element_id_stable_bidirectional(self) -> None:
        scene = route_scene(ROUTE_VERSION_PAYLOAD)
        business_ids = set(scene["node_positions"].keys())
        assert len(scene["node_to_business"]) == len(business_ids)
        # 每个 node_* id 都能映射回业务 ID
        for el in scene["elements"]:
            if el["id"].startswith("node_"):
                assert el["id"] in scene["node_to_business"]

    def test_prereq_arrows_dashed(self) -> None:
        scene = outline_scene(OUTLINE_VERSION_PAYLOAD)
        arrows = [el for el in scene["elements"] if el["type"] == "arrow"]
        assert arrows
        assert all(a.get("strokeStyle") == "dashed" for a in arrows)

    def test_long_chinese_no_overflow(self) -> None:
        long_title = "数据库系统实现与事务并发控制（含中英文 Mixed Terms 与长标题换行观察）"
        inner = LAYOUT["course_w"] - 2 * LAYOUT["node_inner_pad"]
        lines = wrap_label(long_title, inner)
        assert len(lines) <= LAYOUT["max_lines"]
        for line in lines:
            assert measure_text_width(line) <= inner

    def test_wrap_truncates_with_ellipsis(self) -> None:
        very_long = "长" * 120
        lines = wrap_label(very_long, 196)
        assert len(lines) == LAYOUT["max_lines"]
        assert lines[-1].endswith("…")

    def test_scene_error_outside_whitelist(self) -> None:
        with pytest.raises(Exception) as exc:
            validate_scene({"elements": [{"type": "image", "id": "x"}]})
        assert exc.value.code == "ROUTE_STRUCTURE_INVALID"


class TestMigrationParity:
    def test_migration_tables_match_models(self, database) -> None:
        """迁移建的表与行模型元数据逐列一致（防手写迁移漂移）。"""
        from sqlalchemy import inspect

        from backend.planning.models import Base as PlanningBase

        inspector = inspect(database.engine)
        expected = {
            "learning_routes", "route_versions", "route_stages", "route_course_nodes",
            "route_prerequisite_edges", "courses", "outline_versions", "course_units",
            "lessons", "lesson_content_versions", "lesson_prerequisite_edges",
            "planning_drafts", "planning_candidates", "planning_view_states",
            "conversations", "messages", "context_manifests",
            "lesson_progress", "learning_navigation_states", "planning_task_states",
        }
        existing = set(inspector.get_table_names())
        missing = expected - existing
        assert not missing, f"迁移缺表: {missing}"
        for table in expected:
            model_cols = {c.name for c in PlanningBase.metadata.tables[table].columns}
            db_cols = {c["name"] for c in inspector.get_columns(table)}
            assert model_cols <= db_cols, f"{table} 缺列: {model_cols - db_cols}"

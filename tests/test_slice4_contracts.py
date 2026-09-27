"""切片 4 测试：讲次启动门槛、步骤生成、板书契约、TTS 与无音频模式。"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.db import get_database_url  # noqa: E402

os.environ.setdefault("LEARNING_DATABASE_URL", get_database_url())

from backend.teaching.outputs import (  # noqa: E402
    BoardDeclaration,
    LessonPlanCandidate,
    declaration_to_scene,
    validate_step_duration,
)

# 板书声明样例（合成）
GOOD_DECLARATION = {
    "kind": "board",
    "title": "核心概念速览",
    "points": ["定义", "例子", "误用"],
    "code": None,
    "formula": None,
    "nodes": [{"id": "n1", "label": "事务 A"}, {"id": "n2", "label": "事务 B"}],
    "edges": [{"from_id": "n1", "to_id": "n2", "label": "冲突"}],
    "original_refs": [],
    "reveal_ms": [0, 4000, 8000],
}


class TestBoardContract:
    def test_declaration_to_scene_whitelist(self) -> None:
        d = BoardDeclaration.model_validate(GOOD_DECLARATION)
        scene = declaration_to_scene(d)
        for el in scene["elements"]:
            assert el["type"] in {"rectangle", "ellipse", "text", "arrow", "line"}
        # 节点与连线都出现
        types = [el["type"] for el in scene["elements"]]
        assert "rectangle" in types and "arrow" in types
        # original_refs 传递给前端
        assert scene["original_refs"] == []

    def test_scene_is_deterministic(self) -> None:
        d = BoardDeclaration.model_validate(GOOD_DECLARATION)
        assert declaration_to_scene(d) == declaration_to_scene(d)

    def test_edge_unknown_node_rejected(self) -> None:
        import pytest as _pytest

        from backend.planning.errors import PlanningError

        bad = dict(GOOD_DECLARATION)
        bad["edges"] = [{"from_id": "n1", "to_id": "ghost", "label": ""}]
        d = BoardDeclaration.model_validate(bad)  # 结构合法（Pydantic 不校验引用存在性）
        with _pytest.raises(PlanningError) as exc:
            declaration_to_scene(d)  # 场景转换层校验引用
        assert exc.value.code == "BOARD_SCHEMA_INVALID"

    def test_step_duration_hard_cap(self) -> None:
        assert validate_step_duration("字" * 480) == 120000
        import pytest as _pytest

        from backend.planning.errors import PlanningError

        with _pytest.raises(PlanningError) as exc:
            validate_step_duration("字" * 481)
        assert exc.value.code == "BOARD_SCHEMA_INVALID"

    def test_step_candidate_min_length(self) -> None:
        with pytest.raises(Exception):
            LessonPlanCandidate.model_validate({
                "steps": [{"title": "t", "text": "太短", "board": GOOD_DECLARATION}],
            })

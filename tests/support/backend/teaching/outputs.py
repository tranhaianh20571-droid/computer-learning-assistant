"""切片 4 输出契约：教学步骤候选、板书声明与场景转换（T11/T13 冻结）。

- 板书声明是受限 JSON：title / points / code / formula / nodes / edges /
  original_refs / reveal_ms；元素白名单复用切片 3 的 BOARD_ELEMENT_WHITELIST；
  板书输出不得注入任意 HTML、脚本或外部图片 URL；
- 步骤文本 30–60 秒目标（≈4 字/秒 → 120–240 字），120 秒硬上限（≈480 字）
  超限必须拆分；
- 结构图被讲解时必须挂原图资产（原图必显）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator
from .board_blocks import BoardBlock, TeachingBeat, SourceFocus, merge_beats, presentation_blocks
from ..characters.expression import PersonaTraits
from .examples import TeachingExample, ExampleRejection

from ..planning.errors import error
from ..planning.outputs import BOARD_ELEMENT_WHITELIST

# 语速按 4 字/秒估算：30–60 秒目标 → 120–240 字；120 秒硬上限 → 480 字
STEP_MIN_CHARS = 80
STEP_TARGET_MAX_CHARS = 260
STEP_HARD_MAX_CHARS = 480

BOARD_MAX_NODES = 12
BOARD_MAX_EDGES = 12
BOARD_MAX_POINTS = 6
ORIGINAL_REF_LIMIT = 3


class BoardNode(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=60)


class BoardEdge(BaseModel):
    from_id: str = Field(min_length=1, max_length=40)
    to_id: str = Field(min_length=1, max_length=40)
    label: str = Field(default="", max_length=40)


class OriginalRef(BaseModel):
    figure_id: str = Field(min_length=1, max_length=64)
    material_id: str = Field(min_length=1, max_length=64)
    caption: str = Field(default="", max_length=120)


class BoardDeclaration(BaseModel):
    """板书声明（受限 JSON）。元素种类白名单：rect/ellipse/text/arrow/line。"""

    kind: str = Field(default="board")
    format_version: int = Field(default=1, ge=1, le=3)
    title: str = Field(min_length=1, max_length=80)
    blocks: list[BoardBlock] = Field(default_factory=list,max_length=12)
    points: list[str] = Field(default_factory=list, max_length=BOARD_MAX_POINTS)
    code: str | None = Field(default=None, max_length=600)
    formula: str | None = Field(default=None, max_length=200)
    nodes: list[BoardNode] = Field(default_factory=list, max_length=BOARD_MAX_NODES)
    edges: list[BoardEdge] = Field(default_factory=list, max_length=BOARD_MAX_EDGES)
    original_refs: list[OriginalRef] = Field(default_factory=list, max_length=ORIGINAL_REF_LIMIT)
    source_focus: SourceFocus | None = None
    _resolved_source: dict | None = PrivateAttr(default=None)
    reveal_ms: list[int] = Field(default_factory=list)  # 与 points 对齐的呈现时标

    @model_validator(mode="after")
    def valid_blocks(self):
        if len({b.id for b in self.blocks})!=len(self.blocks):
            raise ValueError("duplicate board block ID")
        return self

    @field_validator("kind")
    @classmethod
    def _kind_allowed(cls, v: str) -> str:
        if v != "board":
            raise ValueError("kind must be board")
        return v


class KnowledgeRecapCandidate(BaseModel):
    knowledge_point_id: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=200)
    source_step_refs: list[int] = Field(min_length=1, max_length=24)


class StepCandidate(BaseModel):
    """单步候选：讲解文字 + 板书声明（同版本绑定）。"""

    alignment_version: Literal[0, 1] = 0
    beats: list[TeachingBeat] = Field(default_factory=list,max_length=6)
    text: str = Field(min_length=STEP_MIN_CHARS, max_length=STEP_HARD_MAX_CHARS)
    title: str = Field(min_length=1, max_length=80)
    board: BoardDeclaration
    phase: str = Field(default="", max_length=24)
    covered_goal_ids: list[str] = Field(default_factory=list, max_length=4)
    covered_scope_capability_ids: list[str] = Field(default_factory=list, max_length=240)
    lesson_finished: bool = False
    knowledge_recap: KnowledgeRecapCandidate | None = None
    persona_traits: PersonaTraits | None = None
    examples: list[TeachingExample] = Field(default_factory=list, max_length=2)
    example_rejections: list[ExampleRejection] = Field(default_factory=list, max_length=40)
    _example_review: list[dict] = PrivateAttr(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def derive_text(cls, value):
        if isinstance(value,dict) and value.get("beats") and not value.get("text"):
            value = dict(value)
            value["text"] = "".join(b.get("text","") for b in value["beats"] if isinstance(b,dict))
        return value

    @model_validator(mode="after")
    def synchronized(self):
        if self.beats and self.text != "".join(b.text for b in self.beats):
            raise ValueError("text must equal concatenated teaching beat text")
        merge_beats(self.board,self.beats)
        if self.alignment_version == 1:
            self.board.format_version = 3
            from .board_alignment import scene_beats
            scene_beats(self)
        return self

    @field_validator("text")
    @classmethod
    def _no_control(cls, v: str) -> str:
        cleaned = "".join(ch for ch in v if ch >= " " or ch in "\n\t")
        if len(cleaned) > STEP_HARD_MAX_CHARS:
            raise ValueError("step too long; split into more steps")
        return cleaned


class LessonPlanCandidate(BaseModel):
    """一次生成多步（首段或预生成）；每步独立校验。"""

    steps: list[StepCandidate] = Field(min_length=1, max_length=3)


class FollowupCandidate(BaseModel):
    """追问回答：纯文字 + 可选板书。"""

    alignment_version: Literal[0, 1] = 0
    beats: list[TeachingBeat] = Field(default_factory=list,max_length=6)
    answer: str = Field(min_length=1, max_length=2000)
    board: BoardDeclaration | None = None
    code_instruction: str = Field(default="", max_length=240)

    @model_validator(mode="before")
    @classmethod
    def derive_answer(cls,value):
        if isinstance(value,dict) and value.get("beats") and not value.get("answer"):
            value=dict(value);value["answer"]="".join(b.get("text","") for b in value["beats"] if isinstance(b,dict))
        return value

    @model_validator(mode="after")
    def synchronized(self):
        if self.beats and self.answer != "".join(b.text for b in self.beats):
            raise ValueError("answer must equal concatenated teaching beat text")
        if self.beats and any(b.blocks for b in self.beats) and self.board is None:
            self.board=BoardDeclaration(title="追问板书")
        merge_beats(self.board,self.beats)
        if self.alignment_version == 1:
            if self.board:
                self.board.format_version = 3
            from .board_alignment import scene_beats
            scene_beats(self)
        return self


def validate_step_duration(text: str) -> int:
    """估算步骤时长（毫秒）；超过 120 秒硬上限必须拆分。"""
    seconds = len(text) / 4.0
    if seconds > 120:
        raise error("BOARD_SCHEMA_INVALID", "step exceeds 120s hard cap; split required")
    return int(seconds * 1000)


def declaration_to_scene(declaration: BoardDeclaration, *, layout: dict | None = None) -> dict:
    """板书声明 → 受控 Excalidraw 场景元素（白名单；确定性布局）。

    original_refs 以带标注的矩形 + 标题文本呈现（figure_id 写入元素
    customData 供前端挂原图内容），原图字节经受权接口单独获取。
    """
    elements: list[dict] = []
    y = 40.0
    seq = 0

    def nid(prefix: str) -> str:
        nonlocal seq
        seq += 1
        return f"{prefix}_{seq:03d}"

    elements.append({
        "type": "text", "id": nid("txt"), "x": 60, "y": y,
        "text": declaration.title, "fontSize": 24, "fontFamily": 1,
        "lineHeight": 1.25, "strokeColor": "#1b1b1f", "width": 480, "height": 30,
    })
    y += 48
    for i, point in enumerate(declaration.points):
        elements.append({
            "type": "text", "id": nid("txt"), "x": 60, "y": y,
            "text": f"{i + 1}. {point}", "fontSize": 16, "fontFamily": 1,
            "lineHeight": 1.25, "strokeColor": "#1b1b1f", "width": 520,
            "height": 20,
        })
        y += 30
    if declaration.formula:
        elements.append({
            "type": "text", "id": nid("txt"), "x": 60, "y": y,
            "text": declaration.formula, "fontSize": 18, "fontFamily": 3,
            "lineHeight": 1.25, "strokeColor": "#1b1b1f", "width": 480, "height": 24,
        })
        y += 40
    if declaration.code:
        lines = declaration.code.split("\n")[:10]
        elements.append({
            "type": "rectangle", "id": nid("rect"), "x": 60, "y": y,
            "width": 560, "height": 18 * len(lines) + 16,
            "strokeColor": "#1b1b1f", "backgroundColor": "#f4f4f2", "fillStyle": "solid",
        })
        elements.append({
            "type": "text", "id": nid("txt"), "x": 72, "y": y + 8,
            "text": "\n".join(lines), "fontSize": 14, "fontFamily": 3,
            "lineHeight": 1.25, "strokeColor": "#1b1b1f", "width": 536,
            "height": 18 * len(lines),
        })
        y += 18 * len(lines) + 32
    # 节点与连线（同一行展开）
    node_pos: dict[str, tuple[float, float]] = {}
    for i, node in enumerate(declaration.nodes):
        x = 60 + (i % 3) * 200
        ny = y + (i // 3) * 90
        node_pos[node.id] = (x, ny)
        elements.append({
            "type": "rectangle", "id": nid("rect"), "x": x, "y": ny,
            "width": 170, "height": 56, "strokeColor": "#1b1b1f",
            "backgroundColor": "#e3e9ff", "fillStyle": "solid",
        })
        elements.append({
            "type": "text", "id": nid("txt"), "x": x + 12, "y": ny + 18,
            "text": node.label, "fontSize": 14, "fontFamily": 1,
            "lineHeight": 1.25, "strokeColor": "#1b1b1f", "width": 146, "height": 18,
        })
    if declaration.nodes:
        y += ((len(declaration.nodes) - 1) // 3 + 1) * 90 + 16
    for edge in declaration.edges:
        a = node_pos.get(edge.from_id)
        b = node_pos.get(edge.to_id)
        if not a or not b:
            raise error("BOARD_SCHEMA_INVALID", "edge references unknown node")
        elements.append({
            "type": "arrow", "id": nid("arr"), "x": a[0] + 170, "y": a[1] + 28,
            "points": [[0, 0], [b[0] - a[0] - 170, b[1] - a[1]]],
            "strokeColor": "#8a8f98", "strokeWidth": 1,
        })
    for ref in declaration.original_refs:
        elements.append({
            "type": "rectangle", "id": nid("orig"), "x": 60, "y": y,
            "width": 300, "height": 180, "strokeColor": "#2346C8",
            "backgroundColor": "#eef4ff", "fillStyle": "solid",
            "customData": {"originalRef": {"figure_id": ref.figure_id, "material_id": ref.material_id}},
        })
        elements.append({
            "type": "text", "id": nid("txt"), "x": 72, "y": y + 74,
            "text": f"原图：{ref.caption or ref.figure_id}", "fontSize": 14,
            "fontFamily": 1, "lineHeight": 1.25, "strokeColor": "#2346C8",
            "width": 276, "height": 18,
        })
        y += 196
    for el in elements:
        if el["type"] not in BOARD_ELEMENT_WHITELIST:
            raise error("BOARD_SCHEMA_INVALID", f"element type {el['type']} not allowed")
    return {
        "source_focus": declaration.source_focus.model_dump() if declaration.source_focus else None,
        "pdf_focus": declaration._resolved_source,
        "presentation": {"version":declaration.format_version if declaration.format_version >= 2 else 2,
                         "title":declaration.title,"blocks":presentation_blocks(declaration)},
        "elements": elements,
        "reveal_ms": declaration.reveal_ms,
        "original_refs": [r.model_dump() for r in declaration.original_refs],
        "layout": layout or {},
    }


__all__ = [
    "BoardDeclaration", "StepCandidate", "LessonPlanCandidate", "FollowupCandidate",
    "BoardNode", "BoardEdge", "OriginalRef",
    "STEP_MIN_CHARS", "STEP_TARGET_MAX_CHARS", "STEP_HARD_MAX_CHARS",
    "validate_step_duration", "declaration_to_scene",
]

"""Typed teaching expressions; the browser owns geometry, never model code."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, TypeAdapter, model_validator
from .graphics import Graphic, circuit_table
import math
import re

GRAPH_EXPRESSION_POLICY = (
    "先判断当前任务是否依赖空间连接、时间变化、状态转移或二维布局；"
    "依赖时必须给出可见图形或精确状态/时刻表，不能仅说如图、看波形或列概念清单。"
    "静态逻辑结构用真实电路，时序及反馈优先原资料图或受限时序/状态图，"
    "不能用无环组合电路冒充含记忆或反馈的系统。无法忠实表达时明确指出图形缺口，"
    "不要臆造原图连接或输入波形；若改编，给出完整的新条件并标明改编。"
    "专业图形：讲函数曲线、曲面或数字逻辑时，优先使用type=figure、graphic的结构化图形，span=3。"
    "graphic.kind=plot给bounds.x/y、curves[].expression和points；surface给expression及x/y范围，只展示不出曲面作图题；"
    "circuit给inputs/outputs、gates(id/type/x/y)及wires(source/target/port)。电路须完整且无环，"
    "交叉不等于连接，NOT只有port=0，其余双输入门port=0/1。数学表达式只用x或x/y、有限数字、"
    "+ - * / ^、sin/cos/tan/sqrt/exp/log/abs及pi/e，乘法必须写*，幂指数为绝对值不超过12的字面常数。"
    "极值点坐标必须先算对，图只解释已讲内容；figure的cues.target为figure，quote准确解释表达式或电路。"
    "图表达约束：先明确本图解释的关系，再选择流程、对象关系、时序或状态。节点写对象/操作/状态，"
    "边写它们之间的动作、传递内容或触发条件，不能用无来源的连接凑成完整图。"
    "主流程与反馈必须区分，不能以时间先后冒充因果。判断分支标注不同条件，状态自循环说明触发条件；"
    "双向或平行连线在含义不同且容易混淆时分别给出简短标签。明确的单纯先后关系可以省略标签。"
    "不要用内部节点ID作为可见分组名；group仅用于有教学含义的真实分组。"
    "相同维度比较优先表格，组成职责可用层次表达，不把所有内容画成方框。"
    "未知关系保持未知，必要时用准确文字解释。只输出受限语义结构，不指定位置、坐标或编写绘图代码。"
)


def graph_issues(blocks, existing_ids=()):
    """Generation-only checks; old frozen diagrams remain readable."""
    issues = []
    for block in blocks:
        if block.type != "graph" or block.id in existing_ids:
            continue
        keys = [(e.from_id, e.to_id, e.label.strip()) for e in block.edges]
        if len(keys) != len(set(keys)):
            issues.append("同一对节点之间存在重复的同含义连线，请合并重复关系。")
        if block.diagram == "flowchart":
            for node in block.nodes:
                outgoing = [e for e in block.edges if e.from_id == node.id]
                if node.shape == "decision" and len(outgoing) > 1:
                    labels = [e.label.strip() for e in outgoing]
                    if not all(labels) or len(labels) != len(set(labels)):
                        issues.append("判断节点的不同分支需要非空且不同的条件标签。")
        if block.diagram == "state" and any(e.from_id == e.to_id and not e.label.strip() for e in block.edges):
            issues.append("状态自循环需要标明触发条件。")
    return list(dict.fromkeys(issues))


def has_screen_position_reference(text):
    """Screen coordinates are not a stable reference for responsive board blocks."""
    position = r"(?:左|右)(?:上|下)?(?:侧|边|方)|(?:上|下)(?:侧|边|方|面)"
    subject = r"代码(?:块|示例)?|板书|表格|流程图|关系图|示意图"
    return bool(re.search(rf"(?:{position})(?:的)?(?:这(?:个|段|张|幅))?(?:{subject})", text)
        or re.search(r"\b(?:code|board|table|diagram)\s+(?:on|at|to)\s+(?:the\s+)?(?:left|right|top|bottom)\b", text, re.I))


class BlockBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(default="", max_length=80)
    span: Literal[1, 2, 3] = 1


class TextBlock(BlockBase):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=600)


class TableBlock(BlockBase):
    type: Literal["table"]
    columns: list[str] = Field(min_length=1, max_length=5)
    rows: list[list[str]] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def shape(self):
        if any(not c or len(c)>80 for c in self.columns):
            raise ValueError("table column length invalid")
        if any(len(row)!=len(self.columns) or any(len(cell)>160 for cell in row) for row in self.rows):
            raise ValueError("table rows must match columns and cell budget")
        return self


class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1,max_length=40,pattern=r"^[A-Za-z0-9_-]+$")
    label: str = Field(min_length=1,max_length=80)
    group: str = Field(default="",max_length=40)
    shape: Literal["process", "decision", "terminal"] = "process"


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_id: str = Field(min_length=1,max_length=40)
    to_id: str = Field(min_length=1,max_length=40)
    label: str = Field(default="",max_length=60)


class GraphBlock(BlockBase):
    type: Literal["graph"]
    diagram: Literal["flowchart","relationship","sequence","state"] = "relationship"
    nodes: list[GraphNode] = Field(min_length=1,max_length=12)
    edges: list[GraphEdge] = Field(default_factory=list,max_length=36)

    @model_validator(mode="after")
    def references(self):
        ids = {n.id for n in self.nodes}
        if len(ids)!=len(self.nodes):
            raise ValueError("duplicate graph node")
        if any(e.from_id not in ids or e.to_id not in ids for e in self.edges):
            raise ValueError("graph edge references missing node")
        return self


class CodeBlock(BlockBase):
    type: Literal["code"]
    language: str = Field(default="",max_length=24)
    code: str = Field(min_length=1,max_length=1200)
    active_lines: list[int] = Field(default_factory=list, max_length=12)
    interaction: bool = False
    run_instruction: str = Field(default="", max_length=240)
    change_instruction: str = Field(default="", max_length=240)

    @model_validator(mode="after")
    def lines_exist(self):
        if any(number < 1 or number > len(self.code.splitlines()) for number in self.active_lines):
            raise ValueError("active line outside code")
        if self.language.strip().lower() in ("python", "python3", "py", "javascript", "js",
                                            "java", "c", "cpp", "c++", "cxx", "pytorch", "torch"):
            self.interaction = True
            self.run_instruction = self.run_instruction or "点击运行，观察这段代码的实际结果。"
            self.change_instruction = self.change_instruction or "修改代码中的一项数据，再运行并观察结果变化。"
        return self


class ArrayCell(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=40)
    value: str = Field(max_length=80)
    index: str = Field(default="", max_length=24)
    state: Literal["normal", "focus", "discarded", "found"] = "normal"
    pointer: str = Field(default="", max_length=24)


class ArrayBlock(BlockBase):
    type: Literal["array"]
    cells: list[ArrayCell] = Field(min_length=1, max_length=24)
    note: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def unique_cells(self):
        if len({cell.id for cell in self.cells}) != len(self.cells):
            raise ValueError("duplicate array cell")
        return self


class StackFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=80)
    variables: list[str] = Field(default_factory=list, max_length=6)
    waiting_for: str = Field(default="", max_length=120)
    active: bool = False

    @model_validator(mode="after")
    def bounded_variables(self):
        if any(len(value) > 120 for value in self.variables):
            raise ValueError("stack variable too long")
        return self


class StackBlock(BlockBase):
    type: Literal["stack"]
    frames: list[StackFrame] = Field(min_length=1, max_length=8)
    code_ref: str = Field(default="", max_length=40)

    @model_validator(mode="after")
    def unique_frames(self):
        if len({frame.id for frame in self.frames}) != len(self.frames) or sum(f.active for f in self.frames) > 1:
            raise ValueError("invalid stack frame identity or active frame")
        return self


class SemanticLayer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=200)


class LayersBlock(BlockBase):
    type: Literal["layers"]
    layers: list[SemanticLayer] = Field(min_length=1, max_length=6)

    @model_validator(mode="after")
    def unique_layers(self):
        if len({layer.id for layer in self.layers}) != len(self.layers):
            raise ValueError("duplicate layer")
        return self


class FormulaBlock(BlockBase):
    type: Literal["formula"]
    formula: str = Field(min_length=1,max_length=300)
    format: Literal["latex", "plain"] = "latex"
    plain_text: str = Field(default="", max_length=600)

    @model_validator(mode="after")
    def safe_source(self):
        if self.format == "latex" and re.search(r"\\(?:href|url|html[A-Za-z]*|includegraphics|input|include|style|class|bbox|colorbox)\b", self.formula, re.I):
            raise ValueError("formula command is not allowed")
        return self


class ChartBlock(BlockBase):
    type: Literal["chart"]
    chart: Literal["bar","line","heatmap"]
    labels: list[str] = Field(min_length=1,max_length=12)
    series_labels: list[str] = Field(default_factory=list,max_length=8)
    values: list[list[float]] = Field(min_length=1,max_length=8)
    is_example: bool = False
    source: str = Field(default="",max_length=160)

    @model_validator(mode="after")
    def data(self):
        if not self.is_example and not self.source.strip():
            raise ValueError("chart requires source or explicit teaching example")
        if any(len(label)>60 for label in self.labels+self.series_labels):
            raise ValueError("chart label too long")
        if any(len(row)!=len(self.labels) or any(not math.isfinite(v) for v in row) for row in self.values):
            raise ValueError("chart values must be finite and match labels")
        if self.series_labels and len(self.series_labels)!=len(self.values):
            raise ValueError("chart series labels do not match data")
        return self


class FigureBlock(BlockBase):
    type: Literal["figure"]
    graphic: Graphic

    @model_validator(mode="after")
    def visible_graphic(self):
        if self.graphic.kind == "circuit":
            circuit_table(self.graphic)
        elif self.graphic.kind == "plot" and not (self.graphic.curves or self.graphic.points):
            raise ValueError("plot needs visible geometry")
        if self.graphic.kind == "plot":
            self.graphic.validate_visible()
        return self


BoardBlock = Annotated[TextBlock | TableBlock | GraphBlock | CodeBlock | FormulaBlock | ChartBlock | FigureBlock
                       | ArrayBlock | StackBlock | LayersBlock,
                       Field(discriminator="type")]
BLOCK_ADAPTER = TypeAdapter(BoardBlock)


class BoardCue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    block_id: str = Field(min_length=1, max_length=40)
    target: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_:-]+$")
    quote: str = Field(min_length=1, max_length=2000)
    occurrence: int = Field(default=0, ge=0, le=100)

class SourceFocus(BaseModel):
    material_id: str = Field(min_length=1, max_length=64)
    target_id: str = Field(min_length=1, max_length=64)


class TeachingBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1,max_length=40,pattern=r"^[A-Za-z0-9_-]+$")
    blocks: list[BoardBlock] = Field(default_factory=list,max_length=4)
    text: str = Field(min_length=1,max_length=2000)
    highlight_ids: list[str] = Field(default_factory=list,max_length=12)
    cues: list[BoardCue] = Field(default_factory=list, max_length=100)
    source_focus: SourceFocus | None = None
    _resolved_source: dict | None = PrivateAttr(default=None)


def merge_beats(board, beats):
    """Final text and presentation remain one immutable teaching version."""
    if len({b.id for b in beats})!=len(beats):
        raise ValueError("duplicate teaching beat")
    blocks = list(board.blocks) if board else []
    by_id = {b.id:b for b in blocks}
    if len(by_id)!=len(blocks):
        raise ValueError("duplicate board block")
    available = set(by_id)
    for block in blocks:
        if isinstance(block,GraphBlock):
            available.update(n.id for n in block.nodes)
        if isinstance(block, (ArrayBlock, StackBlock, LayersBlock)):
            available.update(item.id for item in getattr(block, "cells", getattr(block, "frames", getattr(block, "layers", []))))
    for beat in beats:
        for block in beat.blocks:
            if block.id in by_id and by_id[block.id]!=block:
                raise ValueError("conflicting board block ID")
            if block.id not in by_id:
                blocks.append(block);by_id[block.id]=block
            available.add(block.id)
            if isinstance(block,GraphBlock):
                available.update(n.id for n in block.nodes)
            if isinstance(block, (ArrayBlock, StackBlock, LayersBlock)):
                available.update(item.id for item in getattr(block, "cells", getattr(block, "frames", getattr(block, "layers", []))))
        if not set(beat.highlight_ids)<=available:
            raise ValueError("highlight references an element before it is available")
    if len(blocks)>12:
        raise ValueError("too many board blocks")
    for block in blocks:
        if isinstance(block, StackBlock) and block.code_ref and not isinstance(by_id.get(block.code_ref), CodeBlock):
            raise ValueError("stack code_ref must reference a code block in the same board")
    if board:
        board.blocks = blocks
        if beats or blocks:
            board.format_version = 2
    return board


def presentation_blocks(board):
    """Legacy declarations stay readable, with the same generic layout rules."""
    if board.blocks:
        return [b.model_dump() for b in board.blocks]
    blocks = [{"id":f"point_{i}","type":"text","title":"","span":1,"text":p}
              for i,p in enumerate(board.points)]
    if board.code:
        blocks.append({"id":"legacy_code","type":"code","title":"代码示例","span":1,"language":"","code":board.code})
    if board.formula:
        blocks.append({"id":"legacy_formula","type":"formula","title":"公式","span":1,"formula":board.formula})
    if board.nodes:
        blocks.append({"id":"legacy_graph","type":"graph","title":"关系示意","span":1,
            "diagram":"relationship","nodes":[n.model_dump() for n in board.nodes],
            "edges":[e.model_dump() for e in board.edges]})
    return blocks

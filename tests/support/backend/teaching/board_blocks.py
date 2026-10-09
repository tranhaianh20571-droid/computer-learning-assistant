"""Typed teaching expressions; the browser owns geometry, never model code."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
import math
import re


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


BoardBlock = Annotated[TextBlock | TableBlock | GraphBlock | CodeBlock | FormulaBlock | ChartBlock,
                       Field(discriminator="type")]
BLOCK_ADAPTER = TypeAdapter(BoardBlock)


class TeachingBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1,max_length=40,pattern=r"^[A-Za-z0-9_-]+$")
    blocks: list[BoardBlock] = Field(default_factory=list,max_length=4)
    text: str = Field(min_length=1,max_length=2000)
    highlight_ids: list[str] = Field(default_factory=list,max_length=12)


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
    for beat in beats:
        for block in beat.blocks:
            if block.id in by_id and by_id[block.id]!=block:
                raise ValueError("conflicting board block ID")
            if block.id not in by_id:
                blocks.append(block);by_id[block.id]=block
            available.add(block.id)
            if isinstance(block,GraphBlock):
                available.update(n.id for n in block.nodes)
        if not set(beat.highlight_ids)<=available:
            raise ValueError("highlight references an element before it is available")
    if len(blocks)>12:
        raise ValueError("too many board blocks")
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

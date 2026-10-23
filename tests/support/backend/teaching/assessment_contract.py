"""Versioned assessment data. Descriptive tags never impose a type quota."""
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema
from .graphics import QuestionGraphic, circuit_table


class LessonQuota(BaseModel):
    lesson_id: str
    importance: Literal["core", "normal", "light"]
    target_questions: int = Field(ge=5, le=12)
    rationale: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def band(self):
        low, high = {"core": (10, 12), "normal": (7, 9), "light": (5, 6)}[self.importance]
        if not low <= self.target_questions <= high:
            raise ValueError("question count differs from importance band")
        return self


class DataTable(BaseModel):
    columns: list[str] = Field(min_length=1, max_length=8)
    rows: list[list[str]] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def rectangular(self):
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("table must be rectangular")
        if any(len(cell) > 300 for row in self.rows for cell in row):
            raise ValueError("table cell too long")
        return self


class ChartSeries(BaseModel):
    label: str = Field(max_length=80)
    values: list[float] = Field(min_length=2, max_length=100)

    @model_validator(mode="after")
    def finite(self):
        if not all(math.isfinite(value) for value in self.values):
            raise ValueError("chart values must be finite")
        return self


class ChartSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chart_type: Literal["bar", "line", "scatter"]
    x_label: str = Field(max_length=80)
    y_label: str = Field(max_length=80)
    labels: list[str] = Field(min_length=2, max_length=100)
    x_values: list[float] = Field(default_factory=list, max_length=100)
    series: list[ChartSeries] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def aligned(self):
        if any(len(label) > 40 for label in self.labels):
            raise ValueError("chart labels must be short")
        if any(len(s.values) != len(self.labels) for s in self.series):
            raise ValueError("chart series and labels must align")
        if self.chart_type == "scatter" and len(self.x_values) != len(self.labels):
            raise ValueError("scatter requires numeric x values")
        if not all(math.isfinite(value) for value in self.x_values):
            raise ValueError("x values must be finite")
        return self


class AssessmentFigure(BaseModel):
    kind: Literal["none", "material_figure", "generated_chart", "data_table", "generated_figure"] = "none"
    ref: str = Field(default="", max_length=160)
    source: SkipJsonSchema[dict | None] = None
    caption: str = Field(default="", max_length=300)
    task: str = Field(default="", max_length=300)
    chart_spec: ChartSpec | None = None
    table: DataTable | None = None
    graphic: QuestionGraphic | None = None
    variant_of: str = Field(default="", max_length=160)
    variant_kind: Literal["", "same_figure_new_question", "same_question_new_figure",
                          "new_representation"] = ""

    @model_validator(mode="after")
    def complete(self):
        if (self.kind == "generated_figure") != (self.graphic is not None):
            raise ValueError("generated figure requires typed graphic")
        if self.graphic:
            if self.graphic.kind == "circuit":
                circuit_table(self.graphic)
            elif not (self.graphic.curves or self.graphic.points):
                raise ValueError("plot needs visible geometry")
            if self.graphic.kind == "plot":
                self.graphic.validate_visible()
        if self.kind == "material_figure" and not self.ref:
            raise ValueError("material figure needs a reference")
        if self.kind == "generated_chart" and self.chart_spec is None:
            raise ValueError("generated chart needs data")
        if self.kind == "data_table" and self.table is None:
            raise ValueError("table needs data")
        if self.kind != "none" and not self.task.strip():
            raise ValueError("figure must serve an explicit answer task")
        if self.kind != "material_figure" and self.ref:
            raise ValueError("only material figures accept references")
        if self.kind != "generated_chart" and self.chart_spec is not None:
            raise ValueError("unexpected chart")
        if self.kind != "data_table" and self.table is not None:
            raise ValueError("unexpected table")
        return self


class NumericAnswer(BaseModel):
    value: float = Field(allow_inf_nan=False)
    unit: str = Field(default="", max_length=40)
    decimals: int = Field(default=2, ge=0, le=8)
    notation: Literal["decimal", "percent", "grams", "p_value", "exact"] = "decimal"
    absolute_tolerance: float = Field(default=0, ge=0, allow_inf_nan=False)
    relative_tolerance: float = Field(default=0, ge=0, le=1, allow_inf_nan=False)
    comparison: Literal["equal", "less_than"] = "equal"

    @model_validator(mode="after")
    def precision(self):
        if self.notation == "percent" and self.decimals not in (1, 2):
            raise ValueError("percent uses 1 or 2 decimals")
        if self.notation == "grams" and self.decimals != 0:
            raise ValueError("grams use integer precision")
        if self.notation == "p_value":
            if not 0 <= self.value <= 1 or self.decimals != 3:
                raise ValueError("p values use 3 decimals")
            if self.comparison == "less_than" and self.value != .001:
                raise ValueError("p value threshold is 0.001")
        return self


ASSESSMENT_POLICY = (
    "人物卡只决定语气、称呼、幽默和节奏；不得改变题目、证据标准、追问预算、"
    "迁移要求或最终结论。人物风格与准确性冲突时，准确性优先。"
)

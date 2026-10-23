"""Narrow model candidates; the service owns counters and publication."""
from typing import Literal
from pydantic import BaseModel, Field, model_validator
from .learning_outputs import SourceRejection


class FeynmanQuestion(BaseModel):
    question: str = Field(min_length=8, max_length=400)
    criteria: list[str] = Field(min_length=1, max_length=5)
    source_question_id: str = Field(default="", max_length=160)
    source_use: Literal["original", "adapted", "generated"] = "generated"
    source_rejections: list[SourceRejection] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def bounded(self):
        if any(not value.strip() or len(value)>400 for value in self.criteria):
            raise ValueError("evaluation criterion must be a bounded nonempty statement")
        return self


class FeynmanEvaluation(BaseModel):
    decision: Literal["understood", "probe", "review", "uncertain"]
    evidence: list[str] = Field(default_factory=list, max_length=5)
    gaps: list[str] = Field(default_factory=list, max_length=5)
    feedback: str = Field(min_length=1, max_length=300)
    next_question: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def consistent(self):
        if any(len(value) > 400 for value in self.evidence + self.gaps):
            raise ValueError("evaluation evidence and gaps must be bounded")
        if self.decision == "understood" and (not self.evidence or self.gaps):
            raise ValueError("understood requires evidence and no unresolved gaps")
        if self.decision == "probe" and (len(self.next_question.strip()) < 8 or not self.gaps):
            raise ValueError("probe requires one question and a concrete gap")
        if self.decision != "probe" and self.next_question:
            raise ValueError("terminal feedback must not contain a next question")
        return self

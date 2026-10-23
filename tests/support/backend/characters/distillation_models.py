"""Durable, owner-isolated build checkpoints and authored candidates."""
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal
from ..logging_audit.models import Base
from .models import utcnow
from .schemas import Profile


class CharacterBuildRow(Base):
    __tablename__ = "character_builds"
    __table_args__ = (UniqueConstraint("owner_user_id","request_key",name="uq_character_build_key"),)
    build_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("learning_tasks.task_id",ondelete="CASCADE"))
    card_id: Mapped[str] = mapped_column(String(64))
    card_revision: Mapped[int] = mapped_column(Integer)
    request_key: Mapped[str] = mapped_column(String(64))
    request_json: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(String(64))
    config_id: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[int] = mapped_column(Integer)
    sources_json: Mapped[str] = mapped_column(Text,default="[]")
    search_started: Mapped[bool] = mapped_column(Boolean,default=False)
    result_json: Mapped[str] = mapped_column(Text,default="{}")
    accepted_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=utcnow)


class CharacterBuildStageRow(Base):
    __tablename__ = "character_build_stages"
    __table_args__ = (UniqueConstraint("build_id","stage_key",name="uq_character_build_stage"),)
    task_id: Mapped[str] = mapped_column(ForeignKey("learning_tasks.task_id",ondelete="CASCADE"),primary_key=True)
    build_id: Mapped[str] = mapped_column(ForeignKey("character_builds.build_id",ondelete="CASCADE"),index=True)
    stage_key: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    result_json: Mapped[str] = mapped_column(Text,default="{}")
    state_json: Mapped[str] = mapped_column(Text,default="{}",server_default="{}")


class ResearchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    min_documents: int = Field(default=10,ge=3,le=20)
    min_primary: int = Field(default=6,ge=1,le=15)
    max_searches: int = Field(default=8,ge=1,le=12)
    max_reads: int = Field(default=40,ge=1,le=60)
    max_candidates: int = Field(default=60,ge=10,le=100)


class ResearchQuery(BaseModel):
    query: str = Field(min_length=3,max_length=400)
    dimensions: list[Literal["writings","conversations","expression","external","decisions","timeline"]] = Field(min_length=1,max_length=6)


class ResearchPlan(BaseModel):
    queries: list[ResearchQuery] = Field(min_length=1,max_length=6)


class CharacterSubject(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: Literal["real","fictional","original"] = "real"
    work: str = Field(default="",max_length=240)
    version: str = Field(default="",max_length=240)
    story_point: str = Field(default="",max_length=500)
    teaching_extension: bool = True

    @model_validator(mode="after")
    def require_work(self):
        if self.kind=="fictional" and not self.work.strip():
            raise ValueError("fictional subject requires a work")
        return self


class BuildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid",hide_input_in_errors=True)
    mode: Literal["ai","text"]
    topic: str = Field(min_length=1,max_length=120)
    purpose: str = Field(default="人物导师",max_length=1000)
    source_text: str = Field(default="",max_length=180000)
    selected_character: str = Field(default="",max_length=120)
    request_key: str = Field(min_length=1,max_length=64)
    confirmed: bool = False
    search_provider: Literal["anysearch","grok"] = "anysearch"
    research_policy: ResearchPolicy = Field(default_factory=ResearchPolicy)
    reuse_build_id: str = Field(default="",max_length=64)
    subject: CharacterSubject = Field(default_factory=CharacterSubject)
    mentor_evaluation: bool = False
    dialogue: bool = False
    parent_build_id: str = Field(default="",max_length=64)
    message: str = Field(default="",max_length=12000)
    finish: bool = False
    complete_card: bool = False


class ResearchReport(BaseModel):
    body: str = Field(min_length=20,max_length=16000)
    source_ids: list[str] = Field(default_factory=list,max_length=60)
    gaps: list[str] = Field(default_factory=list,max_length=12)
    contradictions: list[str] = Field(default_factory=list,max_length=12)


class SourceAssessment(BaseModel):
    source_id: str = Field(min_length=1,max_length=64)
    kind: Literal["primary","secondary","unknown"]
    evidence: str = Field(default="",max_length=1200)
    reason: str = Field(min_length=1,max_length=1000)
    relevant: bool = True
    dimensions: list[Literal["writings","conversations","expression","external","decisions","timeline"]] = Field(default_factory=list,max_length=6)


class SourceReview(BaseModel):
    assessments: list[SourceAssessment] = Field(min_length=1,max_length=60)


class MindModel(BaseModel):
    name: str = Field(min_length=1,max_length=100)
    mechanism: str = Field(min_length=1,max_length=1800)
    evidence: list[str] = Field(min_length=1,max_length=5)
    applications: list[str] = Field(min_length=1,max_length=4)
    limits: str = Field(min_length=1,max_length=1000)
    tentative: bool = False


class PersonaExtraction(BaseModel):
    models: list[MindModel] = Field(min_length=1,max_length=7)
    heuristics: list[str] = Field(min_length=1,max_length=10)
    expression_dna: str = Field(min_length=20,max_length=5000)
    values_and_tensions: list[str] = Field(min_length=1,max_length=10)
    boundaries: list[str] = Field(min_length=1,max_length=10)
    influences: list[str] = Field(default_factory=list,max_length=10)


class PersonaModels(BaseModel):
    models: list[MindModel] = Field(min_length=1,max_length=7)


class PersonaTraits(BaseModel):
    heuristics: list[str] = Field(min_length=1,max_length=10)
    expression_dna: str = Field(min_length=20,max_length=5000)
    values_and_tensions: list[str] = Field(min_length=1,max_length=10)
    boundaries: list[str] = Field(min_length=1,max_length=10)
    influences: list[str] = Field(default_factory=list,max_length=10)


class BuiltPersona(BaseModel):
    profile: Profile
    skill: str = Field(min_length=200,max_length=48000)


class TestCase(BaseModel):
    kind: Literal["known","edge","voice"]
    question: str = Field(min_length=8,max_length=1000)
    expected_evidence: str = Field(min_length=1,max_length=2000)


class PersonaTests(BaseModel):
    cases: list[TestCase] = Field(min_length=5,max_length=5)


class PersonaReply(BaseModel):
    answer: str = Field(min_length=1,max_length=3000)


class QualityCheck(BaseModel):
    name: str = Field(min_length=1,max_length=120)
    passed: bool
    reason: str = Field(min_length=1,max_length=1000)


class QualityReport(BaseModel):
    checks: list[QualityCheck] = Field(min_length=3,max_length=15)
    weaknesses: list[str] = Field(default_factory=list,max_length=15)


class AcceptanceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    reason: str = Field(min_length=1,max_length=1200)


class PersonaAcceptance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grounding: AcceptanceCheck
    identity_and_version: AcceptanceCheck
    no_fabricated_quotes: AcceptanceCheck
    teaching_accuracy: AcceptanceCheck
    uncertainty: AcceptanceCheck
    role_consistency: AcceptanceCheck
    feynman_protocol: AcceptanceCheck
    weaknesses: list[str] = Field(default_factory=list,max_length=15)

    @property
    def checks(self):
        return [QualityCheck(name=name,**getattr(self,name).model_dump())
            for name in self.__class__.model_fields if name!="weaknesses"]


class MentorAcceptance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    explanation: AcceptanceCheck
    misconception: AcceptanceCheck
    encouragement: AcceptanceCheck
    honesty: AcceptanceCheck
    feynman: AcceptanceCheck


class FastResearch(BaseModel):
    """One shared research pass, with six separately persisted outputs."""
    model_config = ConfigDict(extra="forbid")
    assessments: list[SourceAssessment] = Field(default_factory=list,max_length=10)
    writings: ResearchReport
    conversations: ResearchReport
    expression: ResearchReport
    external: ResearchReport
    decisions: ResearchReport
    timeline: ResearchReport
    extraction: PersonaExtraction
    profile: Profile


class ClassroomReplies(BaseModel):
    model_config = ConfigDict(extra="forbid")
    explanation: str = Field(min_length=1,max_length=2000)
    misconception: str = Field(min_length=1,max_length=2000)
    encouragement: str = Field(min_length=1,max_length=2000)
    honesty: str = Field(min_length=1,max_length=2000)
    feynman: str = Field(min_length=1,max_length=2000)


class FastAcceptance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona: PersonaAcceptance
    mentor: MentorAcceptance


class Refinement(BaseModel):
    revised_skill: str = Field(default="",max_length=48000)
    changes: list[str] = Field(default_factory=list,max_length=12)
    concerns: list[str] = Field(default_factory=list,max_length=12)


class CharacterCandidates(BaseModel):
    candidates: list[str] = Field(min_length=1,max_length=12)

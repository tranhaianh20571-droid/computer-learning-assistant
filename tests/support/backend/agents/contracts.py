"""Trusted run dependencies; SDK mechanisms receive narrow, server-owned context."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import RunUsage

from ..planning.errors import error


@dataclass
class RunBudget:
    """Shared by all output modes in a run. Task accounting can persist each debit."""
    request_limit: int = 8
    wire_limit: int = 8
    tool_limit: int = 16
    deadline: datetime | None = None
    debit: Callable[[str], None] | None = None
    usage: RunUsage = field(default_factory=RunUsage)
    logical_requests: int = 0
    wire_requests: int = 0
    tool_calls: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def check(self) -> None:
        if self.deadline and datetime.now(timezone.utc) >= self.deadline:
            from ..tasks.service import LeaseError
            raise LeaseError("TASK_DEADLINE_EXCEEDED", "task deadline exceeded")

    def consume(self, kind: str) -> None:
        with self._lock:
            self.check()
            if kind == "logical":
                if self.logical_requests >= self.request_limit:
                    raise error("AGENT_LOOP_LIMIT")
                self.logical_requests += 1
            elif kind == "wire":
                if self.wire_requests >= self.wire_limit:
                    raise error("AGENT_LOOP_LIMIT")
                self.wire_requests += 1
            elif kind == "tool":
                if self.tool_calls >= self.tool_limit:
                    raise error("AGENT_LOOP_LIMIT")
                self.tool_calls += 1
            if self.debit:
                self.debit(kind)


@dataclass(frozen=True)
class AgentDeps:
    actor_id: str = ""
    task_id: str = ""
    attempt: int | None = None
    attempt_id: str | None = None
    generation: int | None = None
    config_id: str | None = None
    config_version: int | str | None = None
    disclosure_scope: dict = field(default_factory=dict)
    budget: RunBudget = field(default_factory=RunBudget)
    check_state: Callable[[], None] | None = None
    authorize_payload: Callable[[bytes, str, dict], None] | None = None
    omit_content_trace: bool = False
    run_request_limit: int = 8
    parent_traceparent: str | None = None

    def check(self) -> None:
        self.budget.check()
        if self.check_state:
            self.check_state()


class PromptMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=262144)


@dataclass(frozen=True)
class GenerationInput:
    """Bound prompt snapshot and complete SDK history; candidate types are role-owned."""
    messages: tuple[dict, ...]
    message_history: tuple[ModelMessage, ...] = ()

    def __post_init__(self):
        if not isinstance(self.messages, tuple) or len(self.messages) > 256:
            raise ValueError("bound prompt messages must be a tuple of at most 256 turns")
        validated = tuple(PromptMessage.model_validate(item).model_dump() for item in self.messages)
        if not validated and not self.message_history:
            raise ValueError("bound prompt or native history required")
        object.__setattr__(self, "messages", validated)
        if not isinstance(self.message_history, tuple):
            raise TypeError("native message history must be a tuple")
        from pydantic_ai.messages import ModelRequest, ModelResponse
        if any(not isinstance(item, (ModelRequest, ModelResponse)) for item in self.message_history):
            raise TypeError("native ModelMessage history required")

    @classmethod
    def from_messages(cls, messages: list[dict], message_history=None):
        return cls(tuple(dict(m) for m in messages), tuple(message_history or ()))


@dataclass(frozen=True)
class IntakeInput(GenerationInput):
    pass

@dataclass(frozen=True)
class RoutePlanningInput(GenerationInput):
    pass

@dataclass(frozen=True)
class OutlinePlanningInput(GenerationInput):
    pass

@dataclass(frozen=True)
class LessonPlanningInput(GenerationInput):
    pass

@dataclass(frozen=True)
class TeachingStepInput(GenerationInput):
    pass

@dataclass(frozen=True)
class FollowupInput(GenerationInput):
    pass

@dataclass(frozen=True)
class AssessmentInput(GenerationInput):
    pass

@dataclass(frozen=True)
class EvaluationInput(GenerationInput):
    pass

@dataclass(frozen=True)
class DialogueInput(GenerationInput):
    pass

@dataclass(frozen=True)
class RevisionInput(GenerationInput):
    pass

@dataclass(frozen=True)
class CoverInput(GenerationInput):
    pass

@dataclass(frozen=True)
class ChatInput(GenerationInput):
    max_len: int = 8000

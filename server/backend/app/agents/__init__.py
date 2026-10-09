"""Native PydanticAI execution with fixed role contracts and trusted dependencies."""

from .contracts import AgentDeps, GenerationInput, RunBudget
from .runner import AgentRunResult, execute_agent

__all__ = ["AgentDeps", "GenerationInput", "RunBudget", "AgentRunResult", "execute_agent"]

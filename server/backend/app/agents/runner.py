"""AgentRunner（方案 S1a/S1b）：统一生成入口。

- 门控复用 model_runner 的 ModelCapabilityGate 与 6h 失败缓存
  （ADR-023：Agent 路径输出模式为 tool→prompted 两档；native 仅适用于
  直连 provider，经 FunctionModel 桥无收益，不参与降级链）；
- 内容捕获在桥内（bridge 出口 emit model.call.content）；
- 聚合指标与 model_runner 同形（stage/mode/outcome/duration/error_code）；
- 同步形态（R3 冻结）：run_sync 仅在无事件循环的 worker/请求线程调用；
  禁止在 async 上下文直接调用（上游 #3899 run_sync+子代理死锁未修）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Type

from pydantic import BaseModel

from ..logging_audit.logger import monotonic_ms
from ..logging_audit.metrics import get_model_call_metrics
from ..planning.errors import PlanningError, error
from ..planning.model_runner import ModelCapabilityGate, mark_mode_failure
from .bridge import AdapterBridgeError, make_bridge_model
from .limits import agent_usage_limits, to_controlled_error

BRIDGE_MODES = ("tool", "prompted")
OUTPUT_RETRIES = 2  # 与 model_runner prompted 档重试上限对齐


@dataclass
class AgentRunResult:
    """结构化生成结果（对齐 model_runner.ModelRunResult 的消费字段）。

    payload 为模型类实例；mode 为成功档位；degraded_from 为已降级档位列表。
    """

    payload: Any
    mode: str
    degraded_from: List[str]


def _split_messages(messages: List[dict]) -> tuple[str, str]:
    """适配器形 messages → (instructions, user_prompt)。"""
    system_parts = [str(m["content"]) for m in messages if m.get("role") == "system"]
    user_parts = [str(m["content"]) for m in messages if m.get("role") == "user"]
    return "\n".join(system_parts), "\n\n".join(user_parts)


def _build_agent(adapter: Any, output_type, instructions: str, *, stream: bool = False,
                 output_validator=None, tools=None):
    from pydantic_ai import Agent

    agent = Agent(
        model=make_bridge_model(adapter, stream=stream),
        output_type=output_type,
        instructions=instructions,
        retries=OUTPUT_RETRIES,
    )
    if output_validator is not None:
        agent.output_validator(output_validator)
    for tool in tools or []:
        agent.tool_plain(tool)
    return agent


def _is_bridge_failure(exc: BaseException) -> bool:
    """供应商级失败（桥错误码）立即终止，不进模式降级。"""
    node: Optional[BaseException] = exc
    seen = 0
    while node is not None and seen < 6:
        if isinstance(node, AdapterBridgeError):
            return True
        node = getattr(node, "__cause__", None)
        seen += 1
    return False


def run_structured_agent(
    adapter: Any,
    model_cls: Type[BaseModel],
    messages: List[dict],
    *,
    tool_name: str = "submit_candidate",
    stage: str = "generate",
    max_requests: Optional[int] = None,
    output_validator=None,
    tools=None,
) -> Any:
    """按 tool→prompted 降级跑结构化生成；返回 AgentRunResult。

    output_validator：业务校验钩子（Agent.output_validator），失败抛
    ModelRetry 时框架带错误清单重试（S2 字段级回喂）。
    tools：受控工具函数列表（S3 检索等，tool_plain 注册）。
    """
    from pydantic_ai.output import PromptedOutput, ToolOutput

    gate = ModelCapabilityGate(adapter, tool_name=tool_name)
    candidate_modes = [m for m in gate.choose_modes() if m in BRIDGE_MODES]
    instructions, prompt = _split_messages(messages)
    degraded: List[str] = []
    for mode in candidate_modes:
        output_type = ToolOutput(model_cls) if mode == "tool" else PromptedOutput(model_cls)
        agent = _build_agent(adapter, output_type, instructions,
                             output_validator=output_validator, tools=tools)
        started = monotonic_ms()
        try:
            result = agent.run_sync(prompt, usage_limits=agent_usage_limits(max_requests))
        except Exception as exc:  # noqa: BLE001 — 全部受控化
            get_model_call_metrics().record(
                stage=stage, outcome="failed", mode=mode,
                error_code="AGENT_LOOP_LIMIT" if "limit" in type(exc).__name__.lower() else "MODEL_CAPABILITY_UNAVAILABLE",
                duration_ms=monotonic_ms() - started,
            )
            if _is_bridge_failure(exc):
                raise to_controlled_error(exc) from exc
            # 校验/循环耗尽 → 记失败缓存，降下一档（与 run_structured 同语义；
            # prompted=text 不入缓存，防连锁杀死最终兜底）
            mark_mode_failure(adapter, mode, tool_name)
            degraded.append(mode)
            continue
        get_model_call_metrics().record(
            stage=stage, outcome="succeeded", mode=mode,
            duration_ms=monotonic_ms() - started,
        )
        return AgentRunResult(payload=result.output, mode=mode, degraded_from=list(degraded))
    raise error("MODEL_CAPABILITY_UNAVAILABLE", "all agent modes failed")


def run_chat_agent(
    adapter: Any,
    messages: List[dict],
    *,
    max_len: int,
    stage: str = "generate",
) -> str:
    """即时文字（output_type=str 单档；流式增量由桥 stream_function 承载）。"""
    instructions, prompt = _split_messages(messages)
    agent = _build_agent(adapter, str, instructions, stream=True)
    started = monotonic_ms()
    try:
        result = agent.run_sync(prompt)
    except Exception as exc:  # noqa: BLE001
        get_model_call_metrics().record(
            stage=stage, outcome="failed", mode="prompted",
            error_code="MODEL_CAPABILITY_UNAVAILABLE",
            duration_ms=monotonic_ms() - started,
        )
        raise to_controlled_error(exc) from exc
    get_model_call_metrics().record(
        stage=stage, outcome="succeeded", mode="prompted",
        duration_ms=monotonic_ms() - started,
    )
    text = str(result.output or "").strip()
    if not text:
        raise error("MODEL_CAPABILITY_UNAVAILABLE", "empty chat answer")
    return text[:max_len]


__all__ = [
    "run_structured_agent", "run_chat_agent",
    "BRIDGE_MODES", "OUTPUT_RETRIES",
]

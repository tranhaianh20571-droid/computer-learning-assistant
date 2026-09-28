"""模型运行器（T04）：能力状态 → 输出模式选择 → 结构化输出 + 降级链。

T01 降级默认结论（evidence/slice3-model-capability-spike.md §4）落地：
1. native（JSON Schema 受限生成）→ tool（函数调用）→ prompted（文本 JSON 解析，
   重试上限 2 次）→ 全部不可用时受控错误码 MODEL_CAPABILITY_UNAVAILABLE；
2. 能力状态来自切片 1 `service_configs.capability_status`；unknown 先探测；
   unavailable 直接走降级链；
3. prompted 解析失败计入重试；超上限任务失败，不无限重试；
4. 供应商原始异常不透出，只保留受控错误码。

生产适配器为切片 1 的内容模型适配器（本模块只依赖 `generate(messages, **options)`
与 `probe(capability)` 契约）；测试注入合成适配器。
每次 `adapter.generate` 调用记入 `model.call.*` 聚合指标（无内容字段）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from ..logging_audit.logger import monotonic_ms
from ..logging_audit.metrics import get_model_call_metrics
from .errors import error
from .outputs import PROMPTED_PARSE_RETRIES

OUTPUT_MODES = ("native", "tool", "prompted")
_REQUIRED_CAPABILITY = {"native": "json_schema", "tool": "tool_call", "prompted": "text"}

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class ModelRunResult:
    mode: str
    payload: BaseModel
    attempts: int
    degraded_from: list[str]


class ModelCapabilityGate:
    """按配置能力状态选择输出模式；能力未知时先探测并回报（由调用方持久化）。"""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    def capability_state(self, capability: str) -> str:
        """available | unavailable | unknown（探测失败按 unavailable）。"""
        probe = getattr(self.adapter, "probe", None)
        status = getattr(self.adapter, "capability_status", None)
        if isinstance(status, dict) and status.get(capability) in ("available", "unavailable"):
            return status[capability]
        if probe is None:
            return "unavailable"
        try:
            result = probe(capability)
            return getattr(result, "state", "unavailable")
        except Exception:  # noqa: BLE001 - 探测异常按能力缺失处理
            return "unavailable"

    def choose_modes(self) -> list[str]:
        """按能力状态给出候选模式顺序（native → tool → prompted）。"""
        modes: list[str] = []
        for mode in OUTPUT_MODES:
            state = self.capability_state(_REQUIRED_CAPABILITY[mode])
            if state == "available":
                modes.append(mode)
        # prompted 只依赖 text：text 不可用则模型整体不可用
        if not modes:
            text_state = self.capability_state("text")
            if text_state == "unavailable":
                raise error("MODEL_CAPABILITY_UNAVAILABLE", "model text capability unavailable")
            modes.append("prompted")
        return modes


def _parse_json_payload(raw: Any) -> Optional[dict]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    match = _JSON_BLOCK_RE.search(raw)
    if match:
        try:
            value = json.loads(match.group(0))
            if isinstance(value, dict):
                return value
        except ValueError:
            return None
    return None


def run_structured(
    adapter: Any,
    model_cls: type[BaseModel],
    messages: list[dict],
    *,
    modes: Optional[list[str]] = None,
    schema: Optional[dict] = None,
    tool_name: str = "submit_candidate",
    prompt_suffix: str = "",
    stage: str = "generate",
) -> ModelRunResult:
    """按模式顺序尝试结构化输出；全部失败抛受控错误码。

    adapter.generate(messages, **options) 返回 dict：
      {"content": str|dict, "tool_arguments": str|dict|None}
    合成适配器与切片 1 适配器扩展都遵循该形状。
    """
    gate = ModelCapabilityGate(adapter)
    candidate_modes = modes or gate.choose_modes()
    degraded_from: list[str] = []
    for mode in candidate_modes:
        attempts = 0
        max_attempts = PROMPTED_PARSE_RETRIES + 1 if mode == "prompted" else 1
        last_error: Optional[str] = None
        while attempts < max_attempts:
            attempts += 1
            options = _options_for_mode(mode, schema, tool_name, prompt_suffix, last_error)
            call_started = monotonic_ms()
            try:
                raw = adapter.generate(messages, **options)
            except Exception as exc:  # noqa: BLE001 - 供应商异常 → 受控错误码
                code = getattr(exc, "code", "MODEL_TIMEOUT")
                mapped = "MODEL_TIMEOUT" if code == "timeout" else "MODEL_CAPABILITY_UNAVAILABLE"
                get_model_call_metrics().record(
                    stage=stage, outcome="failed", mode=mode,
                    error_code=mapped, duration_ms=monotonic_ms() - call_started,
                )
                raise error(mapped) from exc
            get_model_call_metrics().record(
                stage=stage, outcome="succeeded", mode=mode,
                duration_ms=monotonic_ms() - call_started,
            )
            payload_dict = _extract_payload(mode, raw)
            if payload_dict is None:
                last_error = "unparseable_output"
                continue
            try:
                payload = model_cls.model_validate(payload_dict)
                return ModelRunResult(
                    mode=mode, payload=payload, attempts=attempts,
                    degraded_from=list(degraded_from),
                )
            except ValidationError:
                last_error = "schema_violation"
                continue
        degraded_from.append(mode)
    raise error("MODEL_CAPABILITY_UNAVAILABLE", "all output modes failed")


def _options_for_mode(
    mode: str, schema: Optional[dict], tool_name: str, prompt_suffix: str, last_error: Optional[str]
) -> dict:
    if mode == "native":
        return {"response_format": {"type": "json_schema", "json_schema": {"name": tool_name, "schema": schema or {}}}}
    if mode == "tool":
        return {
            "tools": [{
                "type": "function",
                "function": {"name": tool_name, "description": "submit structured result",
                             "parameters": schema or {}},
            }],
            "tool_choice": {"type": "function", "function": {"name": tool_name}},
        }
    hint = f"\n\n请严格输出 JSON（{tool_name}）。"
    if last_error:
        hint += f"上一次输出无法使用（{last_error}），请修正为合法 JSON。"
    if prompt_suffix:
        hint += f"\n{prompt_suffix}"
    return {"append_to_last_user_message": hint}


def _extract_payload(mode: str, raw: Any) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    if mode == "tool":
        args = raw.get("tool_arguments")
        if args is None:
            return None
        return _parse_json_payload(args)
    return _parse_json_payload(raw.get("content"))


def run_chat(adapter: Any, messages: list[dict], *, max_len: int, stage: str = "generate") -> str:
    """即时文字输出：结构化不可用时仍须交付纯文字（native 文字输出路径）。"""
    call_started = monotonic_ms()
    try:
        raw = adapter.generate(messages, plain_text=True)
        content = raw.get("content") if isinstance(raw, dict) else raw
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", "MODEL_TIMEOUT")
        mapped = "MODEL_TIMEOUT" if code == "timeout" else "MODEL_CAPABILITY_UNAVAILABLE"
        get_model_call_metrics().record(
            stage=stage, outcome="failed", error_code=mapped,
            duration_ms=monotonic_ms() - call_started,
        )
        raise error(mapped) from exc
    get_model_call_metrics().record(
        stage=stage, outcome="succeeded", duration_ms=monotonic_ms() - call_started,
    )
    text = str(content or "").strip()
    if not text:
        raise error("MODEL_CAPABILITY_UNAVAILABLE", "empty chat answer")
    return text[:max_len]


__all__ = [
    "ModelCapabilityGate", "ModelRunResult", "OUTPUT_MODES",
    "run_structured", "run_chat", "_parse_json_payload",
]

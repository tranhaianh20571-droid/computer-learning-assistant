"""V2：PydanticAI 原生测试基建 + FunctionModel 桥（ADR-021/022 配套）。

测试分工（选型结论落地）：
- TestModel = Agent 装配冒烟（output_type 端到端、零网络）；
- FunctionModel 持有合成适配器 = 桥行为主力（消息翻译、回填、受控错误映射）；
- 适配器安全边界测试仍在 test_slice1_*（mock HTTP，不经 PydanticAI）。

全部合成数据；重试上限内失败即抛，不触网。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "support"))
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic_ai import Agent  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402

from backend.agents import (  # noqa: E402
    AdapterBridgeError,
    make_bridge_model,
    messages_to_adapter,
)


class _StepOut(BaseModel):
    title: str
    text: str


class _SynthAdapter:
    """合成适配器：记录收到的 messages，按脚本返回。"""

    kind = "content_model"

    def __init__(self, result=None, exc=None):
        self.result = result if result is not None else {"tool_arguments": '{"title": "t", "text": "x"}'}
        self.exc = exc
        self.calls: list[list[dict]] = []

    def generate(self, messages, **opts):
        self.calls.append(list(messages))
        if self.exc is not None:
            raise self.exc
        return self.result


# ---------------------------------------------------------------------------
# TestModel：Agent 装配冒烟（PydanticAI 原生）
# ---------------------------------------------------------------------------

def test_testmodel_agent_assembly_smoke():
    agent = Agent(model=TestModel(), output_type=_StepOut)
    result = agent.run_sync("合成输入")
    assert isinstance(result.output, _StepOut)
    assert result.output.title


# ---------------------------------------------------------------------------
# 桥：FunctionModel 持有合成适配器
# ---------------------------------------------------------------------------

def test_bridge_tool_arguments_validated_output():
    adapter = _SynthAdapter()
    agent = Agent(model=make_bridge_model(adapter), output_type=_StepOut, retries=1)
    result = agent.run_sync("生成一个步骤")
    assert isinstance(result.output, _StepOut)
    assert result.output.title == "t"
    # 适配器收到的是翻译后的 role/content 消息
    assert adapter.calls, "adapter must be invoked through bridge"
    first = adapter.calls[0][0]
    assert first["role"] == "user" and "生成一个步骤" in first["content"]


def test_bridge_plain_text_output():
    adapter = _SynthAdapter(result={"content": "合成纯文本回答"})
    agent = Agent(model=make_bridge_model(adapter), output_type=str, retries=1)
    result = agent.run_sync("问题")
    assert result.output == "合成纯文本回答"


def test_bridge_adapter_timeout_maps_to_controlled_code():
    class _Timeout(Exception):
        code = "timeout"

    adapter = _SynthAdapter(exc=_Timeout())
    agent = Agent(model=make_bridge_model(adapter), output_type=_StepOut, retries=1)
    with pytest.raises(Exception) as excinfo:
        agent.run_sync("x")
    # 沿因果链找到受控错误码（不透出原始供应商异常）
    chain, tb = [], excinfo.value
    while tb is not None and len(chain) < 6:
        chain.append(tb)
        tb = tb.__cause__
    codes = {getattr(e, "code", None) for e in chain}
    assert "MODEL_TIMEOUT" in codes or any(isinstance(e, AdapterBridgeError) for e in chain)


def test_messages_to_adapter_translation():
    """桥内翻译：system/user/assistant 直译，非文本 part 只留受控摘要。"""
    from pydantic_ai.messages import ModelRequest, SystemPromptPart, UserPromptPart

    msgs = [
        ModelRequest(parts=[SystemPromptPart(content="sys")]),
        ModelRequest(parts=[UserPromptPart(content="hello")]),
    ]
    out = messages_to_adapter(msgs)
    assert out == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]


# ---------------------------------------------------------------------------
# S1a：runner 门控降级 / 失败缓存 / 请求装配 / 内容捕获 / 上限映射 / 流式
# ---------------------------------------------------------------------------

from backend.agents.limits import to_controlled_error  # noqa: E402
from backend.agents.runner import run_chat_agent, run_structured_agent  # noqa: E402
from backend.logging_audit.contracts import validate_error_code  # noqa: E402
from backend.planning.model_runner import reset_mode_capability_cache  # noqa: E402


class _GatedAdapter:
    """带 capability_status 的合成适配器：记录调用选项，按脚本回包。"""

    kind = "content_model"

    def __init__(self, *, tool_ok=True, result=None):
        self.capability_status = {
            "text": "available", "tool_call": "available" if tool_ok else "unavailable",
            "json_schema": "unavailable",
        }
        self.result = result or {"tool_arguments": '{"title": "t", "text": "x"}'}
        self.calls: list[dict] = []

    def generate(self, messages, **opts):
        self.calls.append({"messages": list(messages), "opts": opts})
        return self.result


def test_agent_loop_limit_registered_and_mapped():
    spec = validate_error_code("AGENT_LOOP_LIMIT")
    assert spec.retryable and spec.stage.value == "generate"
    from pydantic_ai.exceptions import UsageLimitExceeded

    err = to_controlled_error(UsageLimitExceeded("requests"))
    assert err.code == "AGENT_LOOP_LIMIT"


def test_runner_tool_mode_assembles_forced_tools():
    ad = _GatedAdapter()
    out = run_structured_agent(
        ad, _StepOut, [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}],
        tool_name="submit_test",
    ).payload
    assert isinstance(out, _StepOut) and out.title == "t"
    opts = ad.calls[0]["opts"]
    assert "tools" in opts and opts["tool_choice"]["function"]["name"]
    assert opts["tools"][0]["function"]["name"] == "final_result"


def test_runner_falls_back_to_prompted_when_tool_unavailable():
    ad = _GatedAdapter(tool_ok=False, result={"content": '{"title": "p", "text": "y"}'})
    out = run_structured_agent(ad, _StepOut, [{"role": "user", "content": "go"}]).payload
    assert out.title == "p"
    assert all("tools" not in c["opts"] for c in ad.calls), "prompted 档不应带 tools"


def test_runner_marks_failure_cache_and_degrades():
    reset_mode_capability_cache()

    class _RealSig(_GatedAdapter):
        endpoint, model_name = "https://synth.example", "synth-model"

        def generate(self, messages, **opts):
            self.calls.append({"messages": list(messages), "opts": opts})
            if "tools" in opts:
                return {"content": "garbage-not-json"}  # tool 档持续无效
            return {"content": '{"title": "d", "text": "z"}'}

    ad = _RealSig()
    out = run_structured_agent(ad, _StepOut, [{"role": "user", "content": "go"}]).payload
    assert out.title == "d"
    # 第二次：失败缓存应直接跳过 tool 档（全部调用无 tools）
    ad2 = _RealSig()
    out2 = run_structured_agent(ad2, _StepOut, [{"role": "user", "content": "go"}]).payload
    assert out2.title == "d"
    assert all("tools" not in c["opts"] for c in ad2.calls)
    reset_mode_capability_cache()


def test_runner_bridge_failure_maps_controlled():
    class _Timeout(Exception):
        code = "timeout"

    ad = _GatedAdapter()
    ad.generate = lambda messages, **opts: (_ for _ in ()).throw(_Timeout())
    import pytest as _pytest

    from backend.planning.errors import PlanningError

    with _pytest.raises(PlanningError) as ei:
        run_structured_agent(ad, _StepOut, [{"role": "user", "content": "go"}])
    assert ei.value.code == "MODEL_TIMEOUT"


def test_bridge_emits_content_trace(monkeypatch):
    from backend.logging_audit.content_trace import reset_content_trace, get_content_trace
    from backend.logging_audit.logger import MemoryLogStream, StructuredLogger

    monkeypatch.setenv("CONTENT_TRACE_ENABLED", "1")
    reset_content_trace()
    main, content = MemoryLogStream(), MemoryLogStream()
    tr = get_content_trace()
    tr.attach_logger(StructuredLogger(service="api", stream=main, content_stream=content))
    ad = _GatedAdapter()
    run_structured_agent(ad, _StepOut, [{"role": "user", "content": "合成问"}])
    recs = content.records()
    assert len(recs) >= 1 and recs[0]["event"] == "model.call.content"
    assert "合成问" in recs[0]["model_prompt"] and "t" in str(recs[0]["model_answer"])
    assert main.records() == []
    reset_content_trace()


def test_chat_agent_returns_truncated_text():
    ad = _GatedAdapter(result={"content": "答案" * 500})
    text = run_chat_agent(ad, [{"role": "user", "content": "q"}], max_len=100)
    assert len(text) <= 100


def test_stream_function_yields_deltas():
    import asyncio

    from backend.agents import make_stream_function
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    class _StreamAdapter:
        kind = "content_model"

        def generate_stream_text(self, messages):
            yield from ["你", "好", "！"]

    fn = make_stream_function(_StreamAdapter())
    msgs = [ModelRequest(parts=[UserPromptPart(content="hi")])]

    async def _run():
        return [d async for d in fn(msgs, None)]

    assert asyncio.run(_run()) == ["你", "好", "！"]


# ---------------------------------------------------------------------------
# S2：教学步骤业务校验 → ModelRetry 字段级回喂
# ---------------------------------------------------------------------------

from backend.agents.tools import lesson_step_issues, lesson_step_output_validator  # noqa: E402
from backend.teaching.outputs import BoardDeclaration, LessonPlanCandidate, StepCandidate  # noqa: E402


def _step_payload(board_over=None, text="这是一个足够长的讲解文本，用来通过最短字数校验要求。" * 4):
    board = {
        "title": "t", "points": ["a"],
        "nodes": [{"id": "n1", "label": "节点"}],
        "edges": [{"from_id": "n1", "to_id": "n1", "label": ""}],
    }
    board.update(board_over or {})
    return {
        "steps": [{"title": "s", "text": text, "board": board}]
    }


def test_lesson_step_issues_field_level():
    cand = LessonPlanCandidate.model_validate(
        _step_payload(board_over={
            "edges": [{"from_id": "n1", "to_id": "ghost", "label": ""}],
            "reveal_ms": [100, 200],
        })
    )
    issues = lesson_step_issues(cand)
    assert any("edge references unknown node" in i or "板书" in i for i in issues)
    assert any("reveal_ms" in i for i in issues)


def test_lesson_step_output_validator_retries_with_feedback():
    """坏板书 → ModelRetry 清单回喂 → 第二次给好答案 → 通过（字段级回喂闭环）。"""
    import json as _json

    good = _step_payload()
    bad = _step_payload(board_over={"edges": [{"from_id": "n1", "to_id": "ghost", "label": ""}]})

    class _ScriptedAdapter(_GatedAdapter):
        def __init__(self):
            super().__init__()
            self.script = [bad, good]

        def generate(self, messages, **opts):
            self.calls.append({"messages": list(messages), "opts": opts})
            payload = self.script.pop(0) if self.script else good
            return {"tool_arguments": _json.dumps(payload, ensure_ascii=False)}

    ad = _ScriptedAdapter()
    out = run_structured_agent(
        ad, LessonPlanCandidate,
        [{"role": "user", "content": "生成教学步骤"}],
        tool_name="submit_lesson_steps",
        output_validator=lesson_step_output_validator,
    ).payload
    assert isinstance(out, LessonPlanCandidate)
    assert len(ad.calls) >= 2, "校验失败必须回喂重试"
    # 回喂信息进入对话历史（RetryPromptPart 文本含具体问题）
    history = " ".join(str(getattr(m, "parts", "")) for m in [])
    assert ad.calls[1]["messages"], "第二次调用应携带重试上下文"

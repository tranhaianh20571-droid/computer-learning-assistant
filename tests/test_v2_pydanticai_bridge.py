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

"""v2 结构化生成：流式收集、提示注入、模式能力缓存。

合成适配器 + 合成数据；覆盖：
- prompted 模式优先走 generate_stream_text（流式收集），并在重试时把
  "上一次输出无法使用" 修正提示注入最后一条 user 消息（修复 hint 无人消费的缺陷）；
- 真实网关签名（endpoint|model_name）上 native/tool 输出连续无效 → 记入
  能力失败缓存，后续运行直接跳过（缓存优先于过期的配置探测结论）；
- 无签名的合成适配器不受缓存影响（测试隔离）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "support"))

import pytest
from pydantic import BaseModel

from backend.planning.model_runner import (  # noqa: E402
    reset_mode_capability_cache,
    run_structured,
)
from slice3_common import SyntheticModelAdapter  # noqa: E402


class _Payload(BaseModel):
    name: str


def _good_text() -> str:
    return "```json\n" + json.dumps({"name": "ok"}, ensure_ascii=False) + "\n```"


class StreamTextAdapter(SyntheticModelAdapter):
    """prompted 走流式收集的合成适配器。"""

    endpoint = "https://stream.example"
    model_name = "stream-model"

    def __init__(self, *, stream_texts: list[str] | None = None, **kw):
        super().__init__(**kw)
        self.stream_calls = 0
        self.stream_messages: list[list[dict]] = []
        self._stream_texts = list(stream_texts or [])

    def generate_stream_text(self, messages, **options):
        self.stream_calls += 1
        self.stream_messages.append([dict(m) for m in messages])
        text = self._stream_texts.pop(0) if self._stream_texts else _good_text()
        return text


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_mode_capability_cache()
    yield
    reset_mode_capability_cache()


def test_prompted_uses_stream_and_injects_retry_hint():
    adapter = StreamTextAdapter(stream_texts=["这不是 JSON", _good_text()])
    messages = [{"role": "user", "content": "目标：学机器学习"}]
    result = run_structured(adapter, _Payload, messages, modes=["prompted"])
    assert result.mode == "prompted"
    assert result.attempts == 2
    assert adapter.stream_calls == 2
    assert adapter.generate_calls == []  # 未走非流式
    # 重试提示注入到最后一条 user 消息（修复 hint 无人消费的缺陷）
    last = adapter.stream_messages[1][-1]
    assert "上一次输出无法使用" in str(last["content"])
    assert "请严格输出 JSON" in str(last["content"])
    # 原始 messages 不被修改
    assert messages[0]["content"] == "目标：学机器学习"


def test_capability_cache_skips_failed_modes():
    class GwAdapter(SyntheticModelAdapter):
        endpoint = "https://cap.example"
        model_name = "cap-model"

        def generate(self, messages, **options):
            self.generate_calls.append(options)
            return {"content": "抱歉，无法按该格式输出。"}  # native/tool 输出无效

        def generate_stream_text(self, messages, **options):
            self.stream_calls = getattr(self, "stream_calls", 0) + 1
            return _good_text()

    adapter = GwAdapter(payload={"name": "ok"})
    msgs = [{"role": "user", "content": "x"}]
    run1 = run_structured(adapter, _Payload, msgs)
    assert run1.mode == "prompted"
    assert run1.degraded_from == ["native", "tool"]
    native_tool_calls_after_run1 = len(adapter.generate_calls)
    assert native_tool_calls_after_run1 == 2

    run2 = run_structured(adapter, _Payload, msgs)
    assert run2.mode == "prompted"
    assert run2.degraded_from == []  # 缓存命中：直接 prompted，不再降级
    assert len(adapter.generate_calls) == native_tool_calls_after_run1  # 未重试 native/tool


def test_cache_scoped_by_schema_and_text_not_cached():
    """访谈追问的失败不得毒化路线生成（schema 隔离）；text 失败不进缓存。"""

    class Adapter(SyntheticModelAdapter):
        endpoint = "https://shared.example"
        model_name = "shared-model"

        def __init__(self, **kw):
            super().__init__(**kw)
            self.stream_calls = 0

        def generate(self, messages, **options):
            self.generate_calls.append(options)
            # 工具调用要求 IntakeQuestionSet 形状时输出无效（模拟访谈追问失败）
            if options.get("tools"):
                fn = options["tools"][0]["function"]
                if fn["name"] == "submit_intake_questions":
                    return {"content": "抱歉，无法按该格式输出。"}
            return {"content": "抱歉，无法按该格式输出。"}

        def generate_stream_text(self, messages, **options):
            self.stream_calls += 1
            return _good_text()

    adapter = Adapter(payload={"name": "ok"})
    msgs = [{"role": "user", "content": "x"}]

    # 访谈追问链：native/tool/prompted 全败 → text 不缓存
    from backend.planning.outputs import IntakeQuestionSet
    with pytest.raises(Exception) as ei:
        run_structured(
            adapter, IntakeQuestionSet, msgs,
            schema=IntakeQuestionSet.model_json_schema(),
            tool_name="submit_intake_questions",
        )
    assert ei.value.code == "MODEL_CAPABILITY_UNAVAILABLE"
    calls_after_followup = len(adapter.generate_calls)

    # 路线生成：native/tool 失败被缓存（同 schema），prompted 流式成功
    run1 = run_structured(adapter, _Payload, msgs, tool_name="submit_route_candidate")
    assert run1.mode == "prompted"
    calls_after_route1 = len(adapter.generate_calls)

    # 第二次路线生成：native/tool 命中缓存被跳过
    run2 = run_structured(adapter, _Payload, msgs, tool_name="submit_route_candidate")
    assert run2.mode == "prompted" and run2.degraded_from == []
    assert len(adapter.generate_calls) == calls_after_route1
    # 且路线生成未受访谈追问失败影响（追问后仍尝试了 native/tool）
    assert calls_after_route1 > calls_after_followup


def test_no_signature_adapters_not_cached():
    class Broken(SyntheticModelAdapter):
        def generate(self, messages, **options):
            self.generate_calls.append(options)
            return {"content": "抱歉，无法按该格式输出。"}

    adapter = Broken(payload={"name": "ok"})  # 无 endpoint/model_name → 不进缓存
    msgs = [{"role": "user", "content": "x"}]
    for _ in range(2):
        with pytest.raises(Exception) as ei:
            run_structured(adapter, _Payload, msgs)
        assert ei.value.code == "MODEL_CAPABILITY_UNAVAILABLE"
    # 无签名不进缓存：第二次仍完整尝试 native(1)+tool(1)+prompted(3)
    assert len(adapter.generate_calls) == 10

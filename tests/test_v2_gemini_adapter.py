"""v2 GeminiAdapter 生成实现：schema 翻译、消息映射、native/tool/流式。

全部使用 httpx.MockTransport，不调用真实供应商（约定同 test_slice1_adapters.py）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests" / "support"))

import httpx
import pytest

from backend.planning.outputs import RouteCandidate  # noqa: E402
from backend.slice1.adapters.gemini import GeminiAdapter  # noqa: E402

FAKE_KEY = "gemini-test-fake-key-000000"


def _sse_handler(chunks: list[str], record: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append({"url": str(request.url), "body": json.loads(request.content or b"{}")})
        body = "".join(f"data: {c}\n\n" for c in chunks)
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    return handler


def _json_handler(body: dict, record: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append({"url": str(request.url), "body": json.loads(request.content or b"{}")})
        return httpx.Response(200, json=body)

    return handler


def _gemini(handler) -> GeminiAdapter:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, timeout=5)
    return GeminiAdapter(
        endpoint="https://relay.example.test",
        model_name="gemini-3.8-flash",
        credentials={"api_key": FAKE_KEY},
        client=client,
    )


MESSAGES = [
    {"role": "system", "content": "你是学习规划助手。只输出符合要求的 JSON。"},
    {"role": "user", "content": "生成路线 JSON"},
]


class TestSchemaTranslation:
    def test_route_candidate_schema_inlines_refs_and_strips(self):
        adapter = _gemini(None)
        schema = RouteCandidate.model_json_schema()
        translated = adapter._translate_schema(schema)
        text = json.dumps(translated, ensure_ascii=False)
        assert "$ref" not in text and "$defs" not in text
        assert "additionalProperties" not in text
        # 类型转为 Gemini 大写枚举
        assert translated["properties"]["stages"]["type"] == "ARRAY"
        assert translated["type"] == "OBJECT"

    def test_anyof_nullable_becomes_nullable(self):
        adapter = _gemini(None)
        out = adapter._translate_schema({"anyOf": [{"type": "string"}, {"type": "null"}]})
        assert out["type"] == "STRING" and out.get("nullable") is True

    def test_recursive_ref_degrades(self):
        adapter = _gemini(None)
        schema = {
            "type": "object",
            "properties": {"child": {"$ref": "#/$defs/Node"}},
            "$defs": {"Node": {"type": "object", "properties": {"child": {"$ref": "#/$defs/Node"}}}},
        }
        out = adapter._translate_schema(schema)
        assert out["properties"]["child"]["type"] == "OBJECT"


class TestMessagesAndGenerate:
    def test_signed_function_call_survives_pydanticai_bridge_roundtrip(self):
        from backend.agents.bridge import raw_to_model_response, messages_to_adapter, trace_messages
        from pydantic_ai.messages import ModelRequest, ToolReturnPart
        adapter = _gemini(_json_handler({"candidates": [{"content": {"parts": [{
            "functionCall": {"name": "read_context", "args": {}},
            "thoughtSignature": "synthetic_signature"}]}}]}))
        raw = adapter.generate(MESSAGES)
        response = raw_to_model_response(raw)
        call = response.parts[0]
        messages = messages_to_adapter([response, ModelRequest(parts=[ToolReturnPart(
            tool_name="read_context", tool_call_id=call.tool_call_id, content={"ready": True})])])
        contents, _ = adapter._messages_to_contents(messages)
        assert contents[0]["parts"][0]["thoughtSignature"] == "synthetic_signature"
        assert "synthetic_signature" not in json.dumps(trace_messages(messages))

    def test_generate_native_maps_system_and_schema(self):
        record: list = []
        route_json = json.dumps(
            {"stages": [{"title": "阶段一", "courses": [{"title": "课程", "goal": "目标"}]}],
             "prerequisites": []},
            ensure_ascii=False,
        )
        adapter = _gemini(_json_handler(
            {"candidates": [{"content": {"parts": [{"text": route_json}], "role": "model"},
                             "finishReason": "STOP"}]},
            record,
        ))
        result = adapter.generate(MESSAGES, response_format={
            "json_schema": {"name": "route", "schema": RouteCandidate.model_json_schema()},
        })
        assert result["content"] == route_json
        sent = record[0]
        # key 走查询参数
        assert "key=" in sent["url"] and FAKE_KEY in sent["url"]
        assert sent["body"]["systemInstruction"]["parts"][0]["text"].startswith("你是学习规划助手")
        gc = sent["body"]["generationConfig"]
        assert gc["response_mime_type"] == "application/json"
        assert gc["response_schema"]["properties"]["stages"]["type"] == "ARRAY"
        assert sent["body"]["contents"][0]["role"] == "user"

    def test_tool_call_maps_to_tool_arguments(self):
        record: list = []
        fn_args = {"stages": [{"title": "s", "courses": [{"title": "c", "goal": "g"}]}]}
        adapter = _gemini(_json_handler(
            {"candidates": [{"content": {"parts": [{"function_call": {
                "name": "submit_candidate", "args": fn_args}}]}, "finishReason": "STOP"}]},
            record,
        ))
        result = adapter.generate(MESSAGES, tools=[{
            "type": "function",
            "function": {"name": "submit_candidate", "description": "d",
                         "parameters": {"type": "object", "properties": {}}},
        }], tool_choice={"type": "function", "function": {"name": "submit_candidate"}})
        assert result["tool_arguments"] == fn_args
        sent = record[0]
        assert sent["body"]["tools"][0]["function_declarations"][0]["name"] == "submit_candidate"
        assert sent["body"]["tool_config"]["function_calling_config"]["mode"] == "ANY"

    def test_safety_block_returns_empty_for_degradation(self):
        adapter = _gemini(_json_handler(
            {"candidates": [{"finishReason": "SAFETY"}],
             "promptFeedback": {"blockReason": "SAFETY"}},
        ))
        result = adapter.generate(MESSAGES)
        assert result["content"] == "" and result["tool_arguments"] is None
        # 空内容 → 上层 _parse_json_payload 判为不可解析 → 自动降级/失败（不伪造成功）


class TestStreaming:
    def test_generate_stream_text_collects_sse(self):
        chunks = [
            json.dumps({"candidates": [{"content": {"parts": [{"text": "第一段"}]}}]}),
            json.dumps({"candidates": [{"content": {"parts": [{"text": "第二段"}]}}]}),
        ]
        adapter = _gemini(_sse_handler(chunks))
        text = adapter.generate_stream_text(MESSAGES)
        assert text == "第一段第二段"

    def test_stream_prompted_path_via_model_runner(self):
        """model_runner prompted 档在带 generate_stream_text 的适配器上走流式收集。"""
        from backend.planning.model_runner import run_structured

        class StreamGemini(GeminiAdapter):
            endpoint = "https://relay.example.test"
            model_name = "gemini-3.8-flash"
            stream_calls = 0

            def generate_stream_text(self, messages, **options):
                self.stream_calls += 1
                payload = {"stages": [{"title": "阶段", "courses": [{"title": "课", "goal": "目"}]}],
                           "prerequisites": []}
                return json.dumps(payload, ensure_ascii=False)

        adapter = StreamGemini(endpoint="https://relay.example.test", model_name="m",
                               credentials={"api_key": FAKE_KEY},
                               client=httpx.Client(transport=httpx.MockTransport(
                                   lambda r: httpx.Response(200, json={
                                       "candidates": [{"content": {"parts": [{"text": "OK"}],
                                                       "role": "model"}, "finishReason": "STOP"}]})),
                                   timeout=5))
        result = run_structured(
            adapter, RouteCandidate, MESSAGES,
            schema=RouteCandidate.model_json_schema(),
            tool_name="submit_route_candidate",
        )
        assert result.mode == "prompted"
        assert adapter.stream_calls == 1

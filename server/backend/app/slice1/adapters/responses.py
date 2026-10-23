"""Responses connection metadata and optional synthetic probes through the native SDK."""
from .base import AdapterBase, AdapterError, PROBE_TEXT, PROBE_IMAGE_BASE64


class ResponsesAdapter(AdapterBase):
    protocol = "openai_responses"

    def _probe(self, capability):
        import asyncio
        import base64
        from pydantic import BaseModel
        from pydantic_ai import Agent, BinaryContent, NativeOutput, ToolOutput
        from ...agents.contracts import AgentDeps, RunBudget
        from ...agents.native import native_model_session

        class ProbeOutput(BaseModel):
            value: int

        async def run():
            deps = AgentDeps(omit_content_trace=True,
                budget=RunBudget(request_limit=1, wire_limit=1))
            async with native_model_session(self, deps) as model:
                output = (NativeOutput(ProbeOutput) if capability=="json_schema" else
                          ToolOutput(ProbeOutput) if capability=="tool_call" else str)
                agent = Agent(model, output_type=output, retries=0)
                prompt = PROBE_TEXT
                if capability=="image":
                    prompt = [PROBE_TEXT, BinaryContent(
                        data=base64.b64decode(PROBE_IMAGE_BASE64), media_type="image/png")]
                if capability in ("streaming","cancel"):
                    async with agent.run_stream(prompt) as response:
                        async for _ in response.stream_text(delta=True):
                            if capability=="cancel":
                                break
                else:
                    await agent.run(prompt)
        try:
            asyncio.run(run())
        except Exception as exc:
            code = {"MODEL_CREDENTIAL_INVALID":"credential_invalid",
                    "MODEL_TIMEOUT":"timeout","MODEL_CONNECTION_ERROR":"connection_error",
                    "MODEL_RATE_LIMITED":"rate_limited",
                    "MODEL_CAPABILITY_UNAVAILABLE":"unsupported_capability"}.get(getattr(exc,"code",""),"capability_test_failed")
            raise AdapterError(code) from None

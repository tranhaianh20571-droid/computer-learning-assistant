"""Official protocol models with a tenant-isolated, bounded and pinned HTTP exit."""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx2 as httpx
from pydantic_ai.models import Model

from ..planning.errors import PlanningError, error
from ..slice1.adapters.base import AdapterBase, MAX_RESPONSE_BYTES, is_metadata_address
from .contracts import AgentDeps


class ModelCapabilityError(Exception):
    code = "MODEL_CAPABILITY_UNAVAILABLE"


class ReasoningParameterError(PlanningError):
    """Explicit provider rejection of a named reasoning parameter only."""
    def __init__(self, message: str = ""):
        super().__init__("MODEL_CAPABILITY_UNAVAILABLE", "unsupported reasoning parameter")


def _native_reasoning_rejection(detail, request_path: str) -> bool:
    """Messages/generateContent errors name parameters in text, not OpenAI param.

    Inspect bounded text only for classification; never return or log that text.
    Other errors retain the generic provider code and cannot trigger fallback.
    """
    if not isinstance(detail, dict):
        return False
    anthropic = request_path.endswith("/messages") and detail.get("type") == "invalid_request_error"
    gemini = ":generateContent" in request_path or ":streamGenerateContent" in request_path
    gemini = gemini and detail.get("status") == "INVALID_ARGUMENT"
    message = detail.get("message")
    if not (anthropic or gemini) or not isinstance(message, str) or len(message) > 2048:
        return False
    names = (r"\b(?:thinking|thinking\.type|budget_tokens|thinking\.budget_tokens|output_config\.effort)\b"
             if anthropic else r"\b(?:thinkingConfig|thinkingLevel|thinkingBudget|includeThoughts|thinking_config|thinking_level|thinking_budget|include_thoughts)\b")
    rejection = r"\b(?:unsupported|not supported|not support|unknown|unrecognized|invalid|must|cannot|not allowed|only supports?)\b"
    return bool(re.search(names, message, re.IGNORECASE) and re.search(rejection, message, re.IGNORECASE))


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream, limit, deps):
        self.stream, self.limit, self.deps = stream, limit, deps

    async def __aiter__(self):
        size, buffered = 0, bytearray()
        pending = None
        iterator = self.stream.__aiter__()
        loop, flush_at = asyncio.get_running_loop(), 0
        try:
            while True:
                if pending is None:
                    pending = asyncio.create_task(anext(iterator))
                if buffered:
                    done, _ = await asyncio.wait((pending,), timeout=max(0, flush_at-loop.time()))
                    if not done:
                        self.deps.check()
                        yield bytes(buffered)
                        buffered.clear()
                        continue
                try:
                    chunk = await pending
                except StopAsyncIteration:
                    break
                pending = None
                size += len(chunk)
                if size > self.limit:
                    raise error("MODEL_RESPONSE_TOO_LARGE")
                if not buffered:
                    flush_at = loop.time() + .05
                buffered.extend(chunk)
                # Authorize bounded application batches, rather than repeating
                # a full database snapshot for each tiny HTTP/SSE packet.
                while len(buffered) >= 4096:
                    self.deps.check()
                    batch = bytes(buffered[:4096])
                    del buffered[:4096]
                    yield batch
            if buffered:
                self.deps.check()
                yield bytes(buffered)
        except httpx.TimeoutException:
            raise error("MODEL_TIMEOUT") from None
        except (httpx.HTTPError, OSError):
            raise error("MODEL_CONNECTION_ERROR") from None
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def aclose(self):
        await self.stream.aclose()


class SafeModelTransport(httpx.AsyncBaseTransport):
    """Check every actual try and pin the configured model's resolved address.

    Nonpublic destinations are allowed; metadata services remain blocked.
    The approved hostname remains the Host and TLS SNI. No environment proxies,
    redirects, HTTP resubmission, or implicit cross-destination fetches. Only a
    TCP/TLS connection failure may select another address from the same snapshot.
    Mock inner transports are only supplied by isolated protocol tests.
    """
    def __init__(self, endpoint: str, deps: AgentDeps, *, max_bytes=MAX_RESPONSE_BYTES,
                 inner=None, resolver=None, allow_redirect_response=False, inspect_parameter_errors=False):
        self.endpoint = httpx.URL(endpoint)
        self.deps, self.max_bytes = deps, max_bytes
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        self.resolver = resolver or socket.getaddrinfo
        self.allow_redirect_response = allow_redirect_response
        self.inspect_parameter_errors = inspect_parameter_errors

    async def handle_async_request(self, request):
        self.deps.check()
        base = self.endpoint
        if (request.url.scheme, request.url.host, request.url.port) != (base.scheme, base.host, base.port):
            raise error("MODEL_PROVIDER_ERROR", "destination rejected")
        if request.url.scheme not in ("http", "https") or request.url.username or request.url.password:
            raise error("MODEL_PROVIDER_ERROR", "destination rejected")
        if is_metadata_address(base.host):
            raise error("MODEL_PROVIDER_ERROR", "destination rejected")
        prefix = base.path.rstrip("/") + "/"
        if not request.url.path.startswith(prefix):
            raise error("MODEL_PROVIDER_ERROR", "destination rejected")
        body = await request.aread()
        if len(body) > self.max_bytes:
            raise error("QUOTA_EXCEEDED")
        if self.deps.authorize_payload:
            self.deps.authorize_payload(body, str(request.url.copy_with(query=None)), self.deps.disclosure_scope)
        self.deps.budget.consume("wire")
        try:
            port = base.port or (80 if base.scheme == "http" else 443)
            infos = await asyncio.to_thread(self.resolver, base.host, port, 0, socket.SOCK_STREAM)
            addresses = [item[4][0] for item in infos]
            if not addresses or any(is_metadata_address(addr) for addr in addresses):
                raise error("MODEL_PROVIDER_ERROR", "destination rejected")
            addresses = list(dict.fromkeys(str(ipaddress.ip_address(addr)) for addr in addresses))
            for index,address in enumerate(addresses):
                self.deps.check()
                if index:
                    self.deps.budget.consume("wire")
                pinned = request.url.copy_with(host=address)
                headers = request.headers.copy()
                headers["host"] = base.netloc.decode("ascii")
                forwarded = httpx.Request(request.method, pinned, headers=headers, content=body,
                    extensions={**request.extensions, "sni_hostname": base.host.encode("ascii")})
                if self.deps.authorize_payload:
                    self.deps.authorize_payload(body, str(request.url.copy_with(query=None)), self.deps.disclosure_scope)
                try:
                    response = await self.inner.handle_async_request(forwarded)
                    break
                except (httpx.ConnectTimeout,httpx.ConnectError):
                    # Only connection establishment failed: no HTTP payload was sent.
                    if index==len(addresses)-1:
                        raise
            if 300 <= response.status_code < 400 and not self.allow_redirect_response:
                await response.aclose()
                raise error("MODEL_PROVIDER_ERROR", "redirect rejected")
            if response.status_code in (401, 403):
                await response.aclose()
                raise error("MODEL_CREDENTIAL_INVALID")
            if response.status_code == 402:
                await response.aclose()
                raise error("QUOTA_EXCEEDED")
            if response.status_code == 429:
                await response.aclose()
                raise error("MODEL_RATE_LIMITED")
            if self.inspect_parameter_errors and response.status_code in (400, 404, 422):
                # Only bounded, fixed-sample discovery inspects parameter schemas.
                return httpx.Response(response.status_code, headers=response.headers,
                    stream=_BoundedStream(response.stream, self.max_bytes, self.deps),
                    extensions=response.extensions)
            if response.status_code in (400, 404, 422):
                # Only an explicit protocol capability error can select another output mode.
                # A raw response body is never used as user-visible diagnostic text.
                chunks = []
                size = 0
                try:
                    async for chunk in response.aiter_bytes():
                        self.deps.check()
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise error("MODEL_RESPONSE_TOO_LARGE")
                        chunks.append(chunk)
                finally:
                    await response.aclose()
                payload = b"".join(chunks)
                import json
                detail = {}
                try:
                    detail = json.loads(payload).get("error", {})
                    code = detail.get("code") if isinstance(detail, dict) else None
                except (ValueError, AttributeError):
                    code = None
                parameter = detail.get("param", "") if isinstance(detail, dict) else ""
                reasoning_parameters = {"reasoning_effort", "reasoning.effort", "thinking", "thinking.type",
                    "thinking.budget_tokens", "output_config.effort", "generationConfig.thinkingConfig",
                    "generationConfig.thinkingConfig.thinkingLevel", "generationConfig.thinkingConfig.thinkingBudget",
                    "generation_config.thinking_config", "thinking_config", "thinking_config.thinking_budget",
                    "thinking_config.thinking_level"}
                if ((code in {"unsupported_parameter", "unsupported_value", "invalid_parameter", "invalid_value"}
                        and isinstance(parameter, str) and parameter in reasoning_parameters)
                        or _native_reasoning_rejection(detail, request.url.path)):
                    raise ReasoningParameterError("unsupported reasoning parameter")
                output_parameter = (parameter.split(".")[0] if isinstance(parameter, str) else "")
                message = detail.get("message", "") if isinstance(detail, dict) else ""
                named_format_rejection = False
                named_tool_rejection = False
                if (code == "invalid_request_error" and isinstance(message, str) and len(message) <= 512
                        and re.search(r"\bresponse_format\b.{0,80}\b(?:unavailable|unsupported|not supported)\b",
                                      message, re.I)):
                    try:
                        requested_format = json.loads(body).get("response_format", {})
                        named_format_rejection = requested_format.get("type") in ("json_schema", "json_object")
                    except (ValueError, AttributeError):
                        pass
                if (code == "invalid_request_error" and isinstance(message, str) and len(message) <= 512
                        and re.search(r"\b(?:does not support|unsupported|not supported)\b.{0,40}\btool_choice\b",
                                      message, re.I)):
                    try:
                        requested_choice = json.loads(body).get("tool_choice")
                        named_tool_rejection = requested_choice not in (None, "auto")
                    except (ValueError, AttributeError):
                        pass
                if (code in ("unsupported_response_format", "unsupported_tool_choice")
                    or code == "unsupported_parameter" and (output_parameter in
                        ("response_format", "tool_choice", "tools") or parameter in ("output_config.format","text.format"))
                    or named_format_rejection or named_tool_rejection):
                    raise ModelCapabilityError("unsupported output protocol")
                raise error("MODEL_PROVIDER_ERROR")
            if response.status_code >= 500:
                await response.aclose()
                failure = error("MODEL_PROVIDER_ERROR")
                if self.inspect_parameter_errors:
                    failure.provider_status = response.status_code
                raise failure
            return httpx.Response(response.status_code, headers=response.headers,
                stream=_BoundedStream(response.stream, self.max_bytes, self.deps),
                extensions=response.extensions)
        except httpx.TimeoutException:
            raise error("MODEL_TIMEOUT") from None
        except (httpx.HTTPError, OSError, ValueError):
            raise error("MODEL_CONNECTION_ERROR") from None

    async def aclose(self):
        await self.inner.aclose()


@asynccontextmanager
async def native_model_session(source, deps: AgentDeps):
    """A Model is explicit injection; configured adapters supply connection data only."""
    if isinstance(source, Model):
        yield source
        return
    if not isinstance(source, AdapterBase):
        raise error("MODEL_CAPABILITY_UNAVAILABLE", "native model is not configured")
    endpoint = source.endpoint.rstrip("/")
    parsed = urlsplit(endpoint)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise error("MODEL_PROVIDER_ERROR", "destination rejected")
    key = source.credentials.get("api_key", "")
    if not key:
        raise error("MODEL_CREDENTIAL_INVALID")
    protocol = source.protocol
    if protocol in ("openai","openai_responses"):
        from ..slice1.model_catalog import canonical_endpoint
        endpoint = canonical_endpoint(endpoint)
    from ..slice1.reasoning import model_settings_for
    settings = model_settings_for(protocol, source.endpoint, source.model_name,
                                  getattr(source, "reasoning_settings", None))
    if getattr(source, "expression_temperature_supported", False):
        settings = {**(settings or {}), "temperature": source.expression_temperature}
    if protocol == "gemini":
        endpoint = endpoint.removesuffix("/v1").removesuffix("/v1beta")
    elif protocol == "anthropic":
        endpoint = endpoint.removesuffix("/v1")
    transport = SafeModelTransport(endpoint, deps, max_bytes=source.max_response_bytes)
    transport_timeout = (httpx.Timeout(source.timeout,connect=15,write=60,pool=15)
                         if getattr(source,"separate_connect_timeout",False) else source.timeout)
    async with httpx.AsyncClient(transport=transport, timeout=transport_timeout,
            follow_redirects=False, trust_env=False) as http_client:
        if protocol in ("openai","openai_responses"):
            from openai import AsyncOpenAI
            from pydantic_ai.models.openai import OpenAIChatModel,OpenAIResponsesModel
            from pydantic_ai.providers.openai import OpenAIProvider
            client = AsyncOpenAI(api_key=key, base_url=endpoint, http_client=http_client, max_retries=0)
            try:
                model_class=OpenAIResponsesModel if protocol=="openai_responses" else OpenAIChatModel
                if protocol=="openai_responses":
                    settings={**settings,"openai_store":False}
                yield model_class(source.model_name, provider=OpenAIProvider(openai_client=client), settings=settings)
            finally:
                await client.close()
        elif protocol == "anthropic":
            from anthropic import AsyncAnthropic
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider
            client = AsyncAnthropic(api_key=key, base_url=endpoint, http_client=http_client, max_retries=0)
            try:
                yield AnthropicModel(source.model_name, provider=AnthropicProvider(anthropic_client=client), settings=settings)
            finally:
                await client.close()
        elif protocol == "gemini":
            from google.genai import Client
            from google.genai.types import HttpOptions, HttpRetryOptions
            from pydantic_ai.models.google import GoogleModel
            from pydantic_ai.providers.google import GoogleProvider

            def reject_sync(request):
                raise error("MODEL_PROVIDER_ERROR", "synchronous model exit rejected")

            # Google constructs a synchronous client even for async usage. Supply
            # an owned, denied exit so environment proxies cannot affect it.
            with httpx.Client(transport=httpx.MockTransport(reject_sync),
                    trust_env=False, follow_redirects=False) as sync_client:
                client = Client(vertexai=False, api_key=key, http_options=HttpOptions(
                    base_url=endpoint, timeout=int(source.timeout * 1000),
                    httpx_client=sync_client, httpx_async_client=http_client,
                    client_args={"trust_env": False}, async_client_args={"trust_env": False},
                    retry_options=HttpRetryOptions(attempts=1)))
                try:
                    yield GoogleModel(source.model_name, provider=GoogleProvider(client=client), settings=settings)
                finally:
                    await client.aio.aclose()
                    client.close()
        else:
            raise error("MODEL_CAPABILITY_UNAVAILABLE", "unsupported protocol")

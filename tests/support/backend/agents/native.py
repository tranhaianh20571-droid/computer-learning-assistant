"""Official protocol models with a tenant-isolated, bounded and pinned HTTP exit."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx2 as httpx
from pydantic_ai.models import Model

from ..planning.errors import error
from ..slice1.adapters.base import AdapterBase, MAX_RESPONSE_BYTES
from .contracts import AgentDeps


class ModelCapabilityError(Exception):
    code = "MODEL_CAPABILITY_UNAVAILABLE"


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, stream, limit, deps):
        self.stream, self.limit, self.deps = stream, limit, deps

    async def __aiter__(self):
        size = 0
        async for chunk in self.stream:
            self.deps.check()
            size += len(chunk)
            if size > self.limit:
                raise error("MODEL_RESPONSE_TOO_LARGE")
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class SafeModelTransport(httpx.AsyncBaseTransport):
    """Check every actual try, resolve to a public address and connect to that address.

    The approved hostname remains the Host and TLS SNI. No environment proxies,
    redirects, automatic transport retries, or implicit cross-destination fetches.
    Mock inner transports are only supplied by isolated protocol tests.
    """
    def __init__(self, endpoint: str, deps: AgentDeps, *, max_bytes=MAX_RESPONSE_BYTES,
                 inner=None, resolver=None):
        self.endpoint = httpx.URL(endpoint)
        self.deps, self.max_bytes = deps, max_bytes
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0, trust_env=False)
        self.resolver = resolver or socket.getaddrinfo

    async def handle_async_request(self, request):
        self.deps.check()
        base = self.endpoint
        if (request.url.scheme, request.url.host, request.url.port) != (base.scheme, base.host, base.port):
            raise error("MODEL_PROVIDER_ERROR", "destination rejected")
        if request.url.scheme != "https" or request.url.username or request.url.password:
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
            infos = await asyncio.to_thread(self.resolver, base.host, base.port or 443, 0, socket.SOCK_STREAM)
            addresses = [item[4][0] for item in infos]
            if not addresses or any(not ipaddress.ip_address(addr).is_global for addr in addresses):
                raise error("MODEL_PROVIDER_ERROR", "destination rejected")
            pinned = request.url.copy_with(host=addresses[0])
            headers = request.headers.copy()
            headers["host"] = base.netloc.decode("ascii")
            forwarded = httpx.Request(request.method, pinned, headers=headers, content=body,
                extensions={**request.extensions, "sni_hostname": base.host.encode("ascii")})
            self.deps.check()
            if self.deps.authorize_payload:
                self.deps.authorize_payload(body, str(request.url.copy_with(query=None)), self.deps.disclosure_scope)
            response = await self.inner.handle_async_request(forwarded)
            if 300 <= response.status_code < 400:
                await response.aclose()
                raise error("MODEL_PROVIDER_ERROR", "redirect rejected")
            if response.status_code in (401, 403):
                await response.aclose()
                raise error("MODEL_CREDENTIAL_INVALID")
            if response.status_code == 429:
                await response.aclose()
                raise error("MODEL_RATE_LIMITED")
            if response.status_code in (400, 404, 422):
                # Only an explicit protocol capability error can select another output mode.
                # A raw response body is never used as user-visible diagnostic text.
                chunks = []
                size = 0
                try:
                    async for chunk in response.stream:
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
                output_parameter = (detail.get("param", "").split(".")[0]
                    if isinstance(detail, dict) and isinstance(detail.get("param", ""), str) else "")
                if (code in ("unsupported_response_format", "unsupported_tool_choice")
                    or code == "unsupported_parameter" and output_parameter in
                        ("response_format", "tool_choice", "tools", "output_config")):
                    raise ModelCapabilityError("unsupported output protocol")
                raise error("MODEL_PROVIDER_ERROR")
            if response.status_code >= 500:
                await response.aclose()
                raise error("MODEL_PROVIDER_ERROR")
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
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
        raise error("MODEL_PROVIDER_ERROR", "destination rejected")
    key = source.credentials.get("api_key", "")
    if not key:
        raise error("MODEL_CREDENTIAL_INVALID")
    protocol = source.protocol
    if protocol == "gemini":
        endpoint = endpoint.removesuffix("/v1").removesuffix("/v1beta")
    elif protocol == "anthropic":
        endpoint = endpoint.removesuffix("/v1")
    transport = SafeModelTransport(endpoint, deps, max_bytes=source.max_response_bytes)
    async with httpx.AsyncClient(transport=transport, timeout=source.timeout,
            follow_redirects=False, trust_env=False) as http_client:
        if protocol == "openai":
            from openai import AsyncOpenAI
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider
            client = AsyncOpenAI(api_key=key, base_url=endpoint, http_client=http_client, max_retries=0)
            try:
                yield OpenAIChatModel(source.model_name, provider=OpenAIProvider(openai_client=client))
            finally:
                await client.close()
        elif protocol == "anthropic":
            from anthropic import AsyncAnthropic
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider
            client = AsyncAnthropic(api_key=key, base_url=endpoint, http_client=http_client, max_retries=0)
            try:
                yield AnthropicModel(source.model_name, provider=AnthropicProvider(anthropic_client=client))
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
                    yield GoogleModel(source.model_name, provider=GoogleProvider(client=client))
                finally:
                    await client.aio.aclose()
                    client.close()
        else:
            raise error("MODEL_CAPABILITY_UNAVAILABLE", "unsupported protocol")

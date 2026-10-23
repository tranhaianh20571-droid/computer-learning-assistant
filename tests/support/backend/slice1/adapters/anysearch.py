"""Official AnySearch REST exit: pinned DNS, no retries, bounded async I/O."""
from __future__ import annotations

import asyncio
import inspect
import ipaddress
import json
import math
import re
import socket
import threading
import zlib
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Callable
from uuid import UUID

import httpx
from pydantic import SecretStr, ValidationError

from ...search.contracts import (
    AnySearchCredentials, AnySearchRequest, RawSearchEnvelope, SearchProviderError,
    RESPONSE_BYTE_LIMIT,
)
from ...net_policy import addresses_allowed
from ...search.policy import DEFAULT_POLICY
from .base import AdapterBase, ProbeResult

ANYSEARCH_ENDPOINT = "https://api.anysearch.com"
ANYSEARCH_SEARCH_URL = ANYSEARCH_ENDPOINT + "/v1/search"
PROBE_QUERY = "public learning resources"


def _reject_json_constant(value):
    raise ValueError("invalid_search_json")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("invalid_search_json")
        result[key] = value
    return result


def _parse_json(body: bytes):
    """Pre-bound nesting before the stdlib parser allocates a recursive tree."""
    text = body.decode("utf-8")
    depth = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > DEFAULT_POLICY.json_depth:
                raise ValueError("invalid_search_json")
        elif char in "]}":
            depth -= 1
    value = json.loads(text, parse_constant=_reject_json_constant, object_pairs_hook=_unique_object)
    stack = [value]
    nodes = 0
    while stack:
        item = stack.pop()
        nodes += 1
        if nodes > DEFAULT_POLICY.json_nodes:
            raise ValueError("invalid_search_json")
        if isinstance(item, dict):
            nodes += len(item)
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return value


def _request_id(payload) -> UUID | None:
    value = payload.get("request_id") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}", value
    ):
        return None
    return UUID(value)


def _retry_after(value: str | None) -> datetime | None:
    if not value or len(value) > 128:
        return None
    now = datetime.now(timezone.utc)
    try:
        if re.fullmatch(r"\d{1,12}", value):
            return now + timedelta(seconds=int(value))
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            return None
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


async def _blocking_resolve(resolver, *args, **kwargs):
    # DNS cannot be cancelled at the OS boundary. A daemon avoids asyncio.run()
    # waiting for a timed-out default-executor lookup while closing a probe loop.
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def deliver(value, error):
        if not result.done():
            if error is not None:
                result.set_exception(error)
            else:
                result.set_result(value)

    def resolve():
        try:
            value, error = resolver(*args, **kwargs), None
        except Exception as exc:
            value, error = None, exc
        try:
            loop.call_soon_threadsafe(deliver, value, error)
        except RuntimeError:
            pass  # The bounded call's loop has already closed.

    threading.Thread(target=resolve, daemon=True, name="anysearch-dns").start()
    return await result


async def _default_resolver(host, port, *_):
    return await _blocking_resolve(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)


class _PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner, *, resolver, body, check_outbound, before_wire, check_time, owned):
        self.owned = owned
        self.inner = inner
        self.resolver = resolver
        self.body = body
        self.check_outbound = check_outbound
        self.before_wire = before_wire
        self.check_time = check_time

    async def handle_async_request(self, request):
        if (request.method != "POST" or str(request.url) != ANYSEARCH_SEARCH_URL
                or await request.aread() != self.body):
            raise SearchProviderError("SEARCH_REQUEST_REJECTED")
        self.check_time()
        self.check_outbound(self.body, ANYSEARCH_SEARCH_URL)
        try:
            if inspect.iscoroutinefunction(self.resolver):
                infos = await self.resolver("api.anysearch.com", 443, 0, socket.SOCK_STREAM)
            else:
                infos = await _blocking_resolve(self.resolver, "api.anysearch.com", 443, 0, socket.SOCK_STREAM)
                if inspect.isawaitable(infos):
                    infos = await infos
        except (socket.gaierror, OSError):
            raise SearchProviderError("SEARCH_CONNECTION_ERROR", retryable=True, unsent=True) from None
        try:
            addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
        except (ValueError, TypeError, IndexError):
            raise SearchProviderError("SEARCH_REQUEST_REJECTED", unsent=True) from None
        if not addresses_allowed(addresses, from_hostname=True):
            raise SearchProviderError("SEARCH_REQUEST_REJECTED", unsent=True)
        headers = request.headers.copy()
        headers["Host"] = "api.anysearch.com"
        pinned = httpx.Request("POST", request.url.copy_with(host=str(addresses[0])),
                               content=self.body, headers=headers,
                               extensions={**request.extensions, "sni_hostname": b"api.anysearch.com"})
        self.check_time()
        self.check_outbound(self.body, ANYSEARCH_SEARCH_URL)
        # User/task/version guards deliberately remain outside provider exception catches.
        self.before_wire()
        self.check_time()
        try:
            return await self.inner.handle_async_request(pinned)
        except httpx.ConnectError:
            raise SearchProviderError("SEARCH_CONNECTION_ERROR", retryable=True, unsent=True) from None
        except httpx.TimeoutException:
            raise SearchProviderError("SEARCH_TIMEOUT", retryable=True) from None
        except httpx.HTTPError:
            # Read/write/protocol failures may already have reached the provider.
            raise SearchProviderError("SEARCH_CONNECTION_ERROR", retryable=True, unsent=False) from None

    async def aclose(self):
        if self.owned:
            await self.inner.aclose()


class _BorrowedClientTransport(httpx.AsyncBaseTransport):
    """Explicit test injection. Borrowed clients remain caller-owned."""
    def __init__(self, client):
        self.client = client

    async def handle_async_request(self, request):
        return await self.client.send(request, stream=True, auth=None, follow_redirects=False)

    async def aclose(self):
        pass


async def _read_limited(response: httpx.Response, limit: int) -> bytes:
    length = response.headers.get("Content-Length", "")
    if length.isdigit() and int(length) > limit:
        raise SearchProviderError("SEARCH_RESPONSE_TOO_LARGE", status=response.status_code)
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in ("identity", "gzip", "deflate"):
        raise SearchProviderError("SEARCH_OUTPUT_INVALID", status=response.status_code)
    decoder = zlib.decompressobj(31 if encoding == "gzip" else zlib.MAX_WBITS) if encoding != "identity" else None
    chunks = []
    size = 0
    # Preloaded MockTransport responses have already been decoded by httpx.
    if response.is_stream_consumed:
        body = response.content
        if len(body) > limit:
            raise SearchProviderError("SEARCH_RESPONSE_TOO_LARGE", status=response.status_code)
        return body
    async for raw in response.aiter_raw():
        if decoder is not None:
            try:
                chunk = decoder.decompress(raw, limit - size + 1)
            except zlib.error:
                raise SearchProviderError("SEARCH_OUTPUT_INVALID", status=response.status_code) from None
        else:
            chunk = raw
        size += len(chunk)
        if size > limit:
            raise SearchProviderError("SEARCH_RESPONSE_TOO_LARGE", status=response.status_code)
        chunks.append(chunk)
        if decoder is not None and (decoder.unconsumed_tail or decoder.unused_data):
            raise SearchProviderError("SEARCH_OUTPUT_INVALID", status=response.status_code)
    if decoder is not None and not decoder.eof:
        raise SearchProviderError("SEARCH_OUTPUT_INVALID", status=response.status_code)
    return b"".join(chunks)


class AnySearchAdapter(AdapterBase):
    kind = "search"
    protocol = "anysearch"
    capabilities = ("text",)
    default_timeout = 20.0

    def __init__(self, *, endpoint: str = ANYSEARCH_ENDPOINT, model_name: str = "",
                 credentials: AnySearchCredentials | dict | None = None,
                 api_key: SecretStr | None = None, client: httpx.AsyncClient | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, resolver: Callable | None = None,
                 timeout: float | None = None, max_response_bytes: int = RESPONSE_BYTE_LIMIT,
                 probe_guard: Callable[[], None] | None = None) -> None:
        if endpoint != ANYSEARCH_ENDPOINT or model_name != "":
            raise SearchProviderError("SEARCH_REQUEST_REJECTED", unsent=True)
        if client is not None and not isinstance(client, httpx.AsyncClient):
            raise TypeError("AnySearch requires an AsyncClient")
        if client is not None and transport is not None:
            raise ValueError("choose client or transport")
        if api_key is not None and credentials is not None:
            raise ValueError("choose api_key or credentials")
        if api_key is not None and not isinstance(api_key, SecretStr):
            raise SearchProviderError("SEARCH_CREDENTIAL_INVALID", unsent=True)
        try:
            self.credentials = (credentials if isinstance(credentials, AnySearchCredentials)
                                else AnySearchCredentials.model_validate(
                                    {"api_key": api_key} if api_key is not None else credentials))
        except ValidationError:
            raise SearchProviderError("SEARCH_CREDENTIAL_INVALID", unsent=True) from None
        self.endpoint = endpoint
        self.model_name = model_name
        self.timeout = min(timeout if timeout is not None else self.default_timeout, self.default_timeout)
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("invalid_search_timeout")
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= RESPONSE_BYTE_LIMIT:
            raise ValueError("invalid_search_response_limit")
        self.max_response_bytes = max_response_bytes
        self._client = client
        self._transport = transport
        self._resolver = resolver or _default_resolver
        self._probe_guard = probe_guard
        self._closed = False

    async def search(self, request: AnySearchRequest, *,
                     check_outbound: Callable[[bytes, str], None],
                     before_wire: Callable[[], None],
                     remaining_seconds: Callable[[], float]) -> RawSearchEnvelope:
        if not all(callable(item) for item in (check_outbound, before_wire, remaining_seconds)):
            raise TypeError("search requires outbound, wire and deadline guards")
        if self._closed:
            raise SearchProviderError("SEARCH_CONFIG_UNAVAILABLE", unsent=True)
        if not isinstance(request, AnySearchRequest):
            raise TypeError("search requires AnySearchRequest")
        body = request.model_dump_json(exclude_none=True).encode("utf-8")
        started = monotonic()
        wire_reserved = False

        def before_send():
            nonlocal wire_reserved
            before_wire()
            wire_reserved = True

        def check_time():
            remaining = remaining_seconds()
            if not isinstance(remaining, (int, float)) or not math.isfinite(remaining):
                raise ValueError("invalid_search_deadline")
            available = min(float(remaining), self.timeout - (monotonic() - started))
            if available <= 0:
                raise SearchProviderError("SEARCH_TIMEOUT", retryable=True, unsent=not wire_reserved)
            return available

        available = check_time()
        # Header encoding is checked without exposing the key or HTTP library error.
        key = self.credentials.api_key.get_secret_value()
        try:
            key.encode("ascii")
        except UnicodeError:
            raise SearchProviderError("SEARCH_CREDENTIAL_INVALID", unsent=True) from None
        inner = (_BorrowedClientTransport(self._client) if self._client is not None
                 else self._transport if self._transport is not None
                 else httpx.AsyncHTTPTransport(retries=0, trust_env=False))
        transport = _PinnedTransport(inner, resolver=self._resolver, body=body,
                                     check_outbound=check_outbound, before_wire=before_send,
                                     check_time=check_time,
                                     owned=self._client is None and self._transport is None)
        envelope = None
        provider_failure = None
        deadline = asyncio.timeout(available)
        try:
            async with deadline:
                async with httpx.AsyncClient(transport=transport, timeout=available,
                                             trust_env=False, follow_redirects=False) as client:
                    async with client.stream("POST", ANYSEARCH_SEARCH_URL, content=body, headers={
                        "Authorization": "Bearer " + key, "Content-Type": "application/json",
                        "Accept": "application/json", "Accept-Encoding": "gzip, deflate",
                    }) as response:
                        status = response.status_code
                        # Even error responses are read through the same byte/depth bounds.
                        raw = await _read_limited(response, self.max_response_bytes)
                        try:
                            payload = _parse_json(raw)
                        except (ValueError, UnicodeError, RecursionError):
                            payload = None
                        external_id = _request_id(payload)
                        if status != 200:
                            code = ({400: "SEARCH_REQUEST_REJECTED", 415: "SEARCH_REQUEST_REJECTED",
                                     401: "SEARCH_CREDENTIAL_INVALID", 403: "SEARCH_CREDENTIAL_INVALID",
                                     402: "SEARCH_QUOTA_EXCEEDED", 429: "SEARCH_RATE_LIMITED"}.get(status)
                                    or ("SEARCH_PROVIDER_UNAVAILABLE" if status >= 500 else "SEARCH_OUTPUT_INVALID"))
                            provider_failure = SearchProviderError(code, status=status,
                                retryable=code in ("SEARCH_RATE_LIMITED", "SEARCH_PROVIDER_UNAVAILABLE"),
                                external_request_id=external_id,
                                retry_after=_retry_after(response.headers.get("Retry-After")) if status == 429 else None)
                        else:
                            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                            if content_type != "application/json" and not content_type.endswith("+json"):
                                raise SearchProviderError("SEARCH_OUTPUT_INVALID", status=status, external_request_id=external_id)
                            try:
                                envelope = RawSearchEnvelope.model_validate_json(raw)
                            except ValidationError:
                                raise SearchProviderError("SEARCH_OUTPUT_INVALID", status=status, external_request_id=external_id) from None
                            if payload is None or len(envelope.data.results) > request.max_results:
                                raise SearchProviderError("SEARCH_OUTPUT_INVALID", status=status, external_request_id=external_id)
                check_time()
        except TimeoutError:
            if not deadline.expired():
                raise
            raise SearchProviderError("SEARCH_TIMEOUT", retryable=True) from None
        except httpx.TimeoutException:
            raise SearchProviderError("SEARCH_TIMEOUT", retryable=True) from None
        except httpx.HTTPError:
            raise SearchProviderError("SEARCH_CONNECTION_ERROR", retryable=True) from None
        if provider_failure is not None:
            raise provider_failure
        return envelope

    def probe(self, capability: str) -> ProbeResult:
        if capability not in self.capabilities:
            return ProbeResult(capability, "unavailable", "not_applicable")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("search sync probe requires a worker thread")
        base_guard = self._probe_guard or (lambda: None)
        guard_error = None

        def guard():
            nonlocal guard_error
            try:
                base_guard()
            except BaseException as exc:
                guard_error = exc
                raise

        failure = None
        try:
            asyncio.run(self.search(AnySearchRequest(query=PROBE_QUERY, max_results=1),
                check_outbound=lambda *_: guard(), before_wire=guard,
                remaining_seconds=lambda: self.timeout))
        except SearchProviderError as exc:
            if guard_error is not None:
                raise guard_error from None
            failure = exc
        # Always validate again before recording availability, including provider failure.
        guard()
        if failure is not None:
            state = "unavailable" if failure.code == "SEARCH_CREDENTIAL_INVALID" else "unknown"
            return ProbeResult(capability, state, failure.code)
        return ProbeResult(capability, "available")

    def probe_all(self) -> dict[str, dict]:
        return {cap: self.probe(cap).as_dict() for cap in self.capabilities}

    async def aclose(self) -> None:
        self._closed = True

    def close(self) -> None:
        # Every internally owned client was already closed in its original loop.
        self._closed = True

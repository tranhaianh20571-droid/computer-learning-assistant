"""Bounded webpage/video clients and deterministic, exact-host routing."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import httpx2

from ..agents.native import SafeModelTransport
from ..net_policy import addresses_allowed, is_literal_address
from ..planning.errors import error
from .errors import error as material_error

VIDEO_HOSTS = {
    "bilibili.com": "bilibili", "www.bilibili.com": "bilibili", "m.bilibili.com": "bilibili",
    "b23.tv": "bilibili", "youtube.com": "youtube", "www.youtube.com": "youtube",
    "m.youtube.com": "youtube", "youtu.be": "youtube", "www.douyin.com": "douyin",
    "v.douyin.com": "douyin", "www.kuaishou.com": "kuaishou", "v.kuaishou.com": "kuaishou",
}
FILE_EXTENSIONS = ("pdf", "doc", "docx", "pptx", "txt", "md", "markdown")


def classify_url(value):
    try:
        parsed = urlsplit(value.strip())
        if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username
                or parsed.password or len(value) > 8192 or parsed.port not in (None, 80, 443)):
            raise ValueError()
        host = parsed.hostname.lower().rstrip(".")
        if host in ("localhost", "metadata.google.internal") or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and not address.is_global:
            raise ValueError()
        query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                 if k not in ("spm_id_from", "vd_source", "from_spmid") and not k.startswith("utm_")]
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", urlencode(query), parsed.fragment))
    except (TypeError, ValueError):
        raise material_error("invalid_field", "链接格式或目标地址不受支持。") from None
    platform = VIDEO_HOSTS.get(host)
    if ((re.fullmatch(r"(www\.)?google\.[a-z.]+", host) and parsed.path == "/search")
            or host in ("www.bing.com", "bing.com") and parsed.path == "/search"):
        return {"kind": "collection", "platform": "", "url": url}
    if platform:
        concrete = (
            platform == "bilibili" and (host == "b23.tv" or bool(re.fullmatch(r"/video/(?:BV[0-9A-Za-z]+|av\d+)/?", parsed.path)))
            or platform == "youtube" and (host == "youtu.be" or parsed.path.startswith(("/shorts/", "/embed/"))
                or parsed.path == "/watch" and bool(dict(query).get("v")))
            or platform in ("douyin", "kuaishou") and (host.startswith("v.") or "/video/" in parsed.path or "/short-video/" in parsed.path)
        )
        return {"kind": "video" if concrete else "collection", "platform": platform, "url": url}
    suffix = parsed.path.rsplit(".", 1)[-1].lower()
    if suffix in ("png", "jpg", "jpeg", "webp"):
        return {"kind": "image", "platform": "", "url": url}
    if suffix in ("mp4", "webm", "mov", "mkv"):
        return {"kind": "video", "platform": "", "url": url}
    return {"kind": "file" if suffix in FILE_EXTENSIONS else "web", "platform": "", "url": url}


def public_resolver(*args):
    host = args[0] if args else ""
    infos = socket.getaddrinfo(*args)
    if not addresses_allowed([info[4][0] for info in infos],
            from_hostname=not is_literal_address(host)):
        raise ValueError("nonpublic source")
    return infos


def validate_service_endpoint(endpoint, protocol):
    if protocol == "bilinote" and endpoint == "builtin://bilinote":
        return
    parsed = urlsplit(endpoint)
    trusted = {x.rstrip("/") for x in os.environ.get("SOURCE_TRUSTED_SERVICES", "").split(",") if x}
    if endpoint.rstrip("/") in trusted and parsed.scheme in ("http", "https") and not parsed.username and not parsed.password:
        return
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
        raise material_error("invalid_field", "请使用 HTTPS 公网服务或管理员登记的内部服务。")
    try:
        public_resolver(parsed.hostname, 443, 0, socket.SOCK_STREAM)
    except (OSError, ValueError):
        raise material_error("invalid_field", "服务地址无法解析为公网目标。") from None


async def service_json(endpoint, path, key, payload, deps, *, timeout=120, transport=None):
    parsed = urlsplit(endpoint)
    trusted = endpoint.rstrip("/") in {x.rstrip("/") for x in os.environ.get("SOURCE_TRUSTED_SERVICES", "").split(",") if x}
    resolver = None if trusted else public_resolver
    deps.check()
    try:
        async with httpx2.AsyncClient(transport=transport or SafeModelTransport(
                endpoint, deps, max_bytes=64_000_000 if path.startswith("/v1/jobs/") else 12_000_000, resolver=resolver),
                timeout=httpx2.Timeout(timeout, connect=15), follow_redirects=False, trust_env=False) as client:
            response = await client.post(endpoint.rstrip("/") + path, json=payload,
                headers={"Authorization": "Bearer " + key} if key else {})
            if response.status_code >= 400:
                raise error("MODEL_PROVIDER_ERROR")
            data = response.json()
            deps.check()
            return data
    except (ValueError, OSError, httpx2.HTTPError):
        raise error("MODEL_PROVIDER_ERROR") from None


async def scrape(endpoint, key, url, options, deps, *, transport=None):
    routed = classify_url(url)
    if routed["kind"] != "web":
        raise material_error("unsupported_media_type", "此链接不是普通文章网页。")
    # Preflight is not sufficient for self-hosted browser subresources: production
    # Firecrawl must also have a public-only network policy.
    parsed = urlsplit(routed["url"])
    try:
        await asyncio.to_thread(public_resolver, parsed.hostname, parsed.port or 443, 0, socket.SOCK_STREAM)
    except (OSError, ValueError):
        raise material_error("invalid_field", "网页目标不是可访问的公网地址。") from None
    data = await service_json(endpoint, "/v2/scrape", key, {
        "url": routed["url"], "formats": ["markdown", "images"] if options.get("keep_images", True) else ["markdown"], "onlyMainContent": True,
        "timeout": min(90000, int(options.get("timeout", 90)) * 1000),
        "waitFor": int(options.get("wait_ms", 0)),
    }, deps, transport=transport)
    result = data.get("data", {})
    metadata = result.get("metadata", {})
    markdown = result.get("markdown", "")
    if (data.get("success") is not True or metadata.get("statusCode", 200) >= 400
            or not isinstance(markdown, str) or not markdown.strip()
            or len(markdown.encode()) > 10 * 1024 * 1024):
        raise error("OCR_OUTPUT_INVALID", "网页没有返回可用正文。")
    final_url = metadata.get("sourceURL") or metadata.get("url") or routed["url"]
    if classify_url(final_url)["kind"] != "web":
        raise error("OCR_OUTPUT_INVALID")
    challenge = ("verify you are human", "checking your browser", "access denied", "人机验证", "请完成验证")
    if any(x in markdown[:2500].lower() for x in challenge):
        raise error("OCR_OUTPUT_INVALID", "网页要求验证，未取得正文。")
    from .local_documents import parse_text
    state = parse_text(markdown.encode(), "md")
    state.update(unit="paragraph", source_kind="web", source_url=routed["url"],
        final_url=final_url, title=str(metadata.get("title", ""))[:300],
        fetched_at=datetime.now(timezone.utc).isoformat(), provider="firecrawl")
    state["remote_images"] = [value for value in result.get("images", []) if isinstance(value, str)][:32]
    for page in state["page_results"]:
        for block in page["blocks"]:
            block["source_kind"] = "web"
            block["locator"] = {"kind": "paragraph", "line_start": block["line_start"], "line_end": block["line_end"]}
    from .study_sources import build_study_index
    state["study_index"] = build_study_index(state)
    return state


async def fetch_public(url, deps, *, limit=20 * 1024 * 1024, destination=None, timeout=60):
    """Follow each public redirect explicitly, without credentials or cookies."""
    from urllib.parse import urljoin
    current, visited = url, set()
    try:
        for _ in range(6):
            classify_url(current)
            if current in visited:
                raise ValueError()
            visited.add(current)
            parsed = urlsplit(current)
            transport = SafeModelTransport(parsed.scheme + "://" + parsed.netloc, deps,
                max_bytes=limit, resolver=public_resolver, allow_redirect_response=True)
            async with httpx2.AsyncClient(transport=transport, timeout=timeout, trust_env=False,
                    follow_redirects=False) as client:
                async with client.stream("GET", current, headers={"User-Agent": "LearningAssistant/1.0"}) as response:
                    if 300 <= response.status_code < 400:
                        current = urljoin(current, response.headers.get("location", ""))
                        if parsed.scheme == "https" and urlsplit(current).scheme != "https":
                            raise ValueError()
                        continue
                    if response.status_code != 200:
                        raise ValueError()
                    total, parts = 0, []
                    stream = destination.open("wb") if destination else None
                    try:
                        async for chunk in response.aiter_bytes():
                            deps.check()
                            total += len(chunk)
                            if total > limit:
                                raise ValueError()
                            stream.write(chunk) if stream else parts.append(chunk)
                    finally:
                        if stream:
                            stream.close()
                    return (total if destination else b"".join(parts)), response.headers.get("content-type", ""), current
        raise ValueError()
    except (ValueError, OSError, httpx2.HTTPError):
        raise error("OCR_OUTPUT_INVALID", "公开资源读取失败或超过限制。") from None

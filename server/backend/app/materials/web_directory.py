"""Bounded public HTML directory discovery and ordered webpage assembly."""
from __future__ import annotations

import asyncio
import copy
import re
from urllib.parse import urldefrag, urljoin, urlsplit

from lxml import html

from .errors import error
from .source_clients import classify_url, fetch_public

MAX_CHAPTERS = 300
MAX_BOOK_BYTES = 20 * 1024 * 1024


def origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def selected_urls(url, chapters):
    route = classify_url(url)
    if not chapters:
        return []
    if route["kind"] != "web" or len(chapters) > MAX_CHAPTERS:
        raise error("invalid_field", "章节选择超出范围。")
    result = []
    for chapter in chapters:
        target = classify_url(urldefrag(chapter)[0])
        if target["kind"] != "web" or origin(target["url"]) != origin(route["url"]):
            raise error("invalid_field", "章节必须是当前网站的普通网页。")
        if target["url"] not in result:
            result.append(target["url"])
    return result


def parse_directory(payload, url):
    try:
        document = html.fromstring(payload, base_url=url)
    except (ValueError, html.etree.ParserError):
        return {"chapters": [], "truncated": False}
    for node in document.xpath("//script|//style|//template|//noscript"):
        node.drop_tree()
    # Explicit TOCs take precedence over generic site navigation.
    containers = document.xpath(
        "//*[contains(@class,'toctree-wrapper') or contains(@class,'wy-menu-vertical') "
        "or @role='doc-toc' or @id='table-of-contents' or @id='toc' "
        "or contains(concat(' ',normalize-space(@class),' '),' toc ') "
        "or @aria-label='目录' or @aria-label='Table of contents']")
    if not containers:
        containers = document.xpath("//nav|//aside|//ol")
        containers = [node for node in containers if len(node.xpath(
            ".//a[re:test(normalize-space(string(.)), '^([0-9]+[.、 ]|第.+[章节])')]",
            namespaces={"re": "http://exslt.org/regular-expressions"})) >= 2]
    candidates = []
    for container in containers:
        chapters, seen = [], set()
        for anchor in container.xpath(".//a[@href]"):
            href = anchor.get("href", "").strip()
            if not href or href.startswith(("#", "?", "javascript:", "mailto:")):
                continue
            title = re.sub(r"[\x00-\x1f\x7f]", "", " ".join(anchor.itertext())).strip()[:300]
            if not title:
                continue
            try:
                target = classify_url(urldefrag(urljoin(url, href))[0])
            except Exception:
                continue
            if (target["kind"] != "web" or origin(target["url"]) != origin(url)
                    or target["url"] == urldefrag(url)[0] or target["url"] in seen):
                continue
            seen.add(target["url"])
            depth = sum(1 for parent in anchor.iterancestors("li") if parent is not container)
            chapters.append({"url": target["url"], "title": title, "depth": min(5, max(0, depth - 1))})
            if len(chapters) > MAX_CHAPTERS:
                break
        candidates.append(chapters)
    chapters = max(candidates, key=len, default=[])
    return {"chapters": chapters[:MAX_CHAPTERS], "truncated": len(chapters) > MAX_CHAPTERS}


async def discover_directory(url, deps):
    route = classify_url(url)
    if route["kind"] != "web":
        return {"url": route["url"], "chapters": [], "truncated": False}
    try:
        payload, mime, final = await asyncio.wait_for(
            fetch_public(route["url"], deps, limit=2 * 1024 * 1024, timeout=20), timeout=20)
    except TimeoutError:
        raise error("invalid_field", "目录读取超时，请稍后重新发送。") from None
    if origin(final) != origin(route["url"]):
        raise error("invalid_field", "入口跳转到了其他网站，请提交最终网页地址。")
    if mime.split(";")[0].lower() not in ("text/html", "application/xhtml+xml"):
        return {"url": route["url"], "chapters": [], "truncated": False}
    deps.check()
    return {"url": route["url"], **parse_directory(payload, final)}


def assemble_book(url, entries):
    from .study_sources import build_study_index
    state = {"unit": "paragraph", "source_kind": "web", "source_url": url,
             "provider": "firecrawl", "title": "网页章节", "page_results": [],
             "chunks": [], "outline": [], "remote_images": [], "chapter_sources": []}
    size = 0
    for entry in entries:
        source = entry["state"]
        state["chapter_sources"].append({"url": entry["url"], "title": source.get("title", "")})
        state["remote_images"].extend(source.get("remote_images", []))
        mapping = {}
        for original in source.get("page_results", []):
            page = copy.deepcopy(original)
            number = len(state["page_results"]) + 1
            mapping[page["page_no"]] = number
            page.update(page_no=number, source_url=entry["url"])
            for block in page.get("blocks", []):
                size += len(block.get("content", "").encode())
                block["source_url"] = entry["url"]
                block.setdefault("locator", {})["source_url"] = entry["url"]
            state["page_results"].append(page)
        for field in ("chunks", "outline"):
            for original in source.get(field, []):
                value = copy.deepcopy(original)
                value.update(page_no=mapping.get(value.get("page_no"), 1), source_url=entry["url"])
                state[field].append(value)
        if size > MAX_BOOK_BYTES:
            raise error("file_too_large", "所选章节正文超过 20 MiB，请减少章节。")
    state["study_index"] = build_study_index(state)
    return state

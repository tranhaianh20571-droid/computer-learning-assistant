"""Synthetic directory discovery, selected scope and durable multi-page imports."""
import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from backend.materials.errors import MaterialsError
from backend.materials.local_documents import parse_text
from backend.materials.source_models import ImportedSourceRow
from backend.materials.source_router import ImportItemIn
from backend.materials.web_directory import assemble_book, discover_directory, parse_directory, selected_urls
from test_source_imports import configure_web, source_env  # noqa: F401


def test_directory_hierarchy_and_safe_dedup():
    payload = b"""<div class="toctree-wrapper"><ul>
      <li><a href="intro/index.html">1. Introduction</a></li>
      <li><a href="basics/index.html">2. Basics</a><ul>
        <li><a href="basics/data.html">2.1 Data</a></li></ul></li>
      <li><a href="basics/data.html#exercise">duplicate</a></li>
      <li><a href="#heading">anchor</a></li>
      <li><a href="https://other.example.org/">external</a></li>
      <li><a href="//127.0.0.1/private">private</a></li>
      <li><a href="book.pdf">download</a></li>
      <li><a href="javascript:alert(1)">script</a></li></ul></div>"""
    result = parse_directory(payload, "https://example.org/")
    assert [c["depth"] for c in result["chapters"]] == [0, 0, 1]
    assert [c["title"] for c in result["chapters"]] == ["1. Introduction", "2. Basics", "2.1 Data"]
    assert len({c["url"] for c in result["chapters"]}) == 3
    assert not result["truncated"]


def test_plain_article_is_single_page_and_numbered_navigation_supported():
    assert not parse_directory(b'<article><a href="/other">Related article</a></article>',
        "https://example.org/")["chapters"]
    assert len(parse_directory(b'<nav><a href="/a">1. Intro</a><a href="/b">2. Data</a></nav>',
        "https://example.org/")["chapters"]) == 2
    result = parse_directory(('<div id="toc">' + "".join(
        f'<a href="/page/{i}">{i}. Chapter</a>' for i in range(302)) + '</div>').encode(),
        "https://example.org/")
    assert len(result["chapters"]) == 300 and result["truncated"]


@pytest.mark.parametrize("url", [
    "https://other.example.org/a", "http://example.org/a", "https://example.org/a.pdf",
    "https://127.0.0.1/a", "javascript:alert(1)", "https://user:pass@example.org/a",
])
def test_selected_scope_rejects_invalid_pages(url):
    with pytest.raises(MaterialsError):
        selected_urls("https://example.org/", [url])


def test_selected_scope_limits_and_normalizes():
    assert selected_urls("https://example.org/", ["https://example.org/a#x", "https://example.org/a"]) == [
        "https://example.org/a"]
    with pytest.raises(MaterialsError):
        selected_urls("https://example.org/", ["https://example.org/a"] * 301)


def test_discovery_does_not_fetch_video_or_file(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("file and video URLs must not be fetched")
    monkeypatch.setattr("backend.materials.web_directory.fetch_public", forbidden)
    for url in ("https://example.org/book.pdf", "https://www.youtube.com/watch?v=synthetic"):
        assert asyncio.run(discover_directory(url, SimpleNamespace()))["chapters"] == []


def test_discovery_uses_bounded_public_fetch(monkeypatch):
    calls = []
    async def fetch(url, deps, **kwargs):
        calls.append(kwargs)
        return b'<div id="toc"><a href="/intro">Intro</a></div>', "text/html; charset=utf-8", url
    monkeypatch.setattr("backend.materials.web_directory.fetch_public", fetch)
    result = asyncio.run(discover_directory("https://example.org/", SimpleNamespace(check=lambda: None)))
    assert result["chapters"][0]["url"] == "https://example.org/intro"
    assert calls == [{"limit": 2 * 1024 * 1024, "timeout": 20}]


def test_directory_endpoint_requires_authentication_and_never_caches(monkeypatch):
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from backend.materials.source_router import create_source_router
    calls = []
    async def discover(url, deps):
        deps.check()
        calls.append(deps.actor_id)
        return {"url": url, "chapters": [], "truncated": False}
    monkeypatch.setattr("backend.materials.web_directory.discover_directory", discover)
    def resolve(request):
        if request.headers.get("X-Synthetic-User") != "member":
            raise HTTPException(status_code=401)
        return SimpleNamespace(user_id="synthetic-member")
    app = FastAPI()
    app.include_router(create_source_router(SimpleNamespace(), resolve))
    with TestClient(app) as client:
        assert client.post("/api/source-directory", json={"url": "https://example.org/"}).status_code == 401
        assert not calls
        result = client.post("/api/source-directory", json={"url": "https://example.org/"},
            headers={"X-Synthetic-User": "member"})
        assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
        assert calls == ["synthetic-member"]


def test_unknown_submission_is_not_replayed_on_reclaim(source_env, monkeypatch):
    env = source_env
    configure_web(env)
    batch = env.service.create(env.owner, uuid.uuid4().hex, [ImportItemIn(client_id="book",
        url="https://example.org/", chapter_urls=["https://example.org/intro"])])
    prepared = env.service.prepare(env.owner, batch["batch_id"])
    started = env.service.commit(env.owner, batch["batch_id"], prepared["manifest_hash"], True)
    material = started["items"][0]["material_id"]
    with env.db.session() as session:
        row = session.get(ImportedSourceRow, material)
        row.result_json = json.dumps({"web_entries": [], "web_pending_attempt": 1,
            "web_total": 1, "web_completed": 0})
        task = row.task_id
    async def forbidden(*args, **kwargs):
        pytest.fail("unknown submission must wait for explicit retry")
    monkeypatch.setattr("backend.materials.source_imports.scrape", forbidden)
    env.service.run(task, env.owner, material)
    assert env.service.view(env.owner, batch["batch_id"])["items"][0]["status"] == "failed"


def test_selected_book_revocation_blocks_remaining_pages(source_env, monkeypatch):
    env = source_env
    profile = configure_web(env)
    calls = []
    async def scrape(endpoint, key, url, options, deps):
        deps.check()
        calls.append(url)
        env.configs.deactivate(profile["configuration"]["config_id"], actor_id=env.owner, is_admin=False)
        return parse_text(b"# Synthetic\n\nTransactions.", "md")
    monkeypatch.setattr("backend.materials.source_imports.scrape", scrape)
    batch = env.service.create(env.owner, uuid.uuid4().hex, [ImportItemIn(client_id="book",
        url="https://example.org/", chapter_urls=["https://example.org/first", "https://example.org/second"])])
    prepared = env.service.prepare(env.owner, batch["batch_id"])
    started = env.service.commit(env.owner, batch["batch_id"], prepared["manifest_hash"], True)
    material = started["items"][0]["material_id"]
    with env.db.session() as session:
        task = session.get(ImportedSourceRow, material).task_id
    env.service.run(task, env.owner, material)
    assert calls == ["https://example.org/first"]
    assert env.service.view(env.owner, batch["batch_id"])["status"] != "ready"


def test_book_combines_order_and_retains_chapter_locations():
    from backend.materials.corpus import segments
    entries = [{"url": f"https://example.org/{name}", "state": parse_text(
        f"# {name}\n\nWhy is {name} useful?\n\n{name} has synthetic source content.".encode(), "md")}
        for name in ("Atomicity", "Isolation")]
    result = assemble_book("https://example.org/", entries)
    assert [p["page_no"] for p in result["page_results"]] == list(range(1, 7))
    assert result["study_index"]["sections"][0]["source_url"] == entries[0]["url"]
    assert result["study_index"]["sections"][1]["source_url"] == entries[1]["url"]
    assert {chunk["source_url"] for chunk in segments(result, "synthetic-material")} == {
        entry["url"] for entry in entries}
    assert entries[1]["state"]["page_results"][0]["page_no"] == 1


def test_multi_page_failure_explicit_retry_reuses_completed_pages(source_env, monkeypatch):
    env = source_env
    configure_web(env)
    calls = []
    fail = [True]
    async def scrape(endpoint, key, url, options, deps):
        deps.check()
        calls.append(url)
        if url.endswith("/second") and fail[0]:
            raise ValueError("synthetic")
        return parse_text(f"# Synthetic {url.rsplit('/', 1)[-1]}\n\nSynthetic transactions.".encode(), "md")
    monkeypatch.setattr("backend.materials.source_imports.scrape", scrape)
    urls = ["https://example.org/first", "https://example.org/second"]
    batch = env.service.create(env.owner, uuid.uuid4().hex, [
        ImportItemIn(client_id="book", url="https://example.org/", chapter_urls=urls)])
    prepared = env.service.prepare(env.owner, batch["batch_id"])
    assert prepared["items"][0]["chapter_count"] == 2
    started = env.service.commit(env.owner, batch["batch_id"], prepared["manifest_hash"], True)
    material = started["items"][0]["material_id"]
    def run():
        with env.db.session() as session:
            task = session.get(ImportedSourceRow, material).task_id
        env.service.run(task, env.owner, material)
    run()
    failed = env.service.view(env.owner, batch["batch_id"])
    assert failed["items"][0]["status"] == "failed" and failed["items"][0]["web_completed"] == 1
    from backend.tasks.service import LeaseError
    with pytest.raises(LeaseError, match="status=failed"):
        run()
    assert calls == urls
    fail[0] = False
    env.service.retry(env.owner, batch["batch_id"], prepared["manifest_hash"], True)
    run()
    assert calls == [*urls, urls[1]]
    ready = env.service.view(env.owner, batch["batch_id"])
    assert ready["status"] == "ready" and ready["items"][0]["web_completed"] == 2
    with env.db.session() as session:
        state = json.loads(session.get(ImportedSourceRow, material).result_json)
    assert [entry["url"] for entry in state["chapter_sources"]] == urls


def test_chapter_selection_changes_import_identity(source_env):
    env = source_env
    key = uuid.uuid4().hex
    env.service.create(env.owner, key, [ImportItemIn(client_id="book", url="https://example.org/",
        chapter_urls=["https://example.org/first"])])
    with pytest.raises(MaterialsError):
        env.service.create(env.owner, key, [ImportItemIn(client_id="book", url="https://example.org/",
            chapter_urls=["https://example.org/second"])])

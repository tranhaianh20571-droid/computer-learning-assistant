"""Authenticated internal Hindsight provider gateway, routed by immutable bank."""
from __future__ import annotations

import asyncio
import hmac
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select

from ..slice1.errors import error
from .personal import MEMORY_STORAGE_DIMENSIONS, MemoryBankRoute, PersonalModels
from .models import MemorySettingsRow, MemorySpaceRow
from .providers import ProviderClient

STORAGE_DIMENSIONS = MEMORY_STORAGE_DIMENSIONS


def create_proxy_router(db):
    router = APIRouter(prefix="/api/internal/memory", tags=["internal-memory"])

    def authenticated(request):
        from .runtime import personal_runtime
        secret = personal_runtime()["proxy_key"]
        actual = request.headers.get("authorization", "")
        if not secret or not hmac.compare_digest(actual, "Bearer " + secret):
            raise error("access_denied")

    def resolve(bank):
        with db.session() as session:
            route = session.get(MemoryBankRoute, bank) if isinstance(bank, str) else None
            if not route:
                raise error("access_denied")
            owner, revision = route.owner_user_id, route.revision
        spec, _ = resolve_snapshot(db, bank, owner, revision)
        if spec["embedding_space"] != route.embedding_space:
            raise error("disclosure_revoked")
        def guard():
            current, _ = resolve_snapshot(db, bank, owner, revision)
            if current["embedding_space"] != route.embedding_space:
                raise error("disclosure_revoked")
        return ProviderClient(spec, guard=guard)

    async def body(request):
        authenticated(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 2_000_000:
                raise error("invalid_field")
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise error("invalid_field") from None

    @router.post("/v1/embeddings")
    async def embeddings(request: Request):
        data = await body(request)
        bank = data.get("user")
        client = resolve(bank)
        if client.spec.get("embedding_status") == "pending":
            await asyncio.to_thread(PersonalModels(db).detect,
                client.spec["owner"], client.spec["revision"])
            client = resolve(bank)
        texts = data.get("input")
        if isinstance(texts, str):
            texts = [texts]
        if not isinstance(texts, list) or not texts or any(not isinstance(t, str) for t in texts):
            raise error("invalid_field")
        storage = client.spec.get("memory_dimensions") or STORAGE_DIMENSIONS
        vectors = await asyncio.to_thread(client.embed, texts)
        if len(vectors[0]) > storage:
            if client.spec.get("memory_compatible") is False:
                raise error("capability_test_failed", "该模型不支持缩短到记忆库维度。")
            vectors = await asyncio.to_thread(client.embed, texts,
                dimensions=storage, expected=storage)
        if len(vectors[0]) > storage:
            raise error("capability_test_failed", "Embedding 维度超过记忆库上限。")
        return {"object": "list", "model": "personal-memory",
            "data": [{"object": "embedding", "index": i,
                "embedding": v + [0.0] * (storage - len(v))}
                for i, v in enumerate(vectors)]}

    @router.post("/v1/rerank")
    async def rerank(request: Request):
        data = await body(request)
        client = resolve(request.headers.get("x-hindsight-bank-id"))
        query, documents = data.get("query"), data.get("documents")
        if not isinstance(query, str) or not isinstance(documents, list) or any(
                not isinstance(t, str) for t in documents):
            raise error("invalid_field")
        result = await asyncio.to_thread(client.rerank, query, documents)
        return {"results": [{"index": i, "relevance_score": score} for i, score in result]}

    @router.post("/v1/chat/completions")
    async def chat(request: Request):
        data = await body(request)
        client = resolve(data.get("user"))
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages or len(messages) > 128:
            raise error("invalid_field")
        options = {k: v for k, v in data.items() if k in
                   ("temperature", "max_tokens", "response_format", "tools", "tool_choice")}
        if "max_completion_tokens" in data and "max_tokens" not in options:
            options["max_tokens"] = data["max_completion_tokens"]
        result = await asyncio.to_thread(client.chat, messages, **options)
        if data.get("stream"):
            async def chunks():
                # Preserve the OpenAI stream contract without exposing provider IDs.
                for choice in result.get("choices", []):
                    item = {"id": "memory-result", "object": "chat.completion.chunk",
                        "created": 0, "model": "personal-memory", "choices": [{
                            "index": choice.get("index", 0), "delta": choice.get("message", {}),
                            "finish_reason": None}]}
                    yield "data: " + json.dumps(item) + "\n\n"
                    item["choices"][0].update(delta={}, finish_reason=choice.get("finish_reason", "stop"))
                    yield "data: " + json.dumps(item) + "\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(chunks(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-store"})
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    return router


def resolve_snapshot(db, bank, owner, revision):
    from ..auth.models import UserRow
    from .personal import MemoryMigration
    from .models import MemorySourceRow
    with db.session() as session:
        route = session.get(MemoryBankRoute, bank)
        space = session.scalar(select(MemorySpaceRow).where(
            MemorySpaceRow.bank_id == bank, MemorySpaceRow.owner_user_id == owner))
        job = None
        if not space:
            job = session.scalar(select(MemoryMigration).where(
                MemoryMigration.owner_user_id == owner, MemoryMigration.target_bank == bank,
                MemoryMigration.revision == revision,
                MemoryMigration.status.in_(("pending", "replaying", "refreshing"))))
            space = session.get(MemorySpaceRow, job.space_id) if job else None
            if space and (space.epoch != job.epoch or space.bank_id != job.source_bank):
                space = None
        setting = session.get(MemorySettingsRow, owner)
        user = session.get(UserRow, owner)
        if (not route or route.owner_user_id != owner or route.revision != revision
                or not space or space.status != "active" or not setting or not setting.enabled
                or not user or user.status != "approved"):
            raise error("disclosure_revoked")
        if session.scalar(select(MemorySourceRow.source_id).where(
                MemorySourceRow.owner_user_id == owner, MemorySourceRow.scope_key == space.scope_key,
                MemorySourceRow.status.in_(("forgetting", "forget_failed", "suppressed_pending"))).limit(1)):
            raise error("disclosure_revoked")
        if job:
            refs = session.execute(select(MemorySourceRow.source_id, MemorySourceRow.revision,
                MemorySourceRow.tag).where(MemorySourceRow.owner_user_id == owner,
                    MemorySourceRow.scope_key == space.scope_key, MemorySourceRow.status.in_(("completed", "compacted")))
                .order_by(MemorySourceRow.source_id)).all()
            if [list(r) for r in refs] != json.loads(job.source_refs_json):
                raise error("disclosure_revoked")
    return PersonalModels(db).resolve(owner, revision), space

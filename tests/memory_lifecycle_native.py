"""Opt-in native PostgreSQL/store protocol check, with synthetic model decisions.

Run with the pinned hindsight-api interpreter. It creates its own pg0 instance,
never reads product credentials, and stops only the instance it starts.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
import uuid


async def run():
    run_id = uuid.uuid4().hex
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    for key in list(os.environ):
        if key.startswith("HINDSIGHT_"):
            del os.environ[key]
    os.environ.update({
        "HINDSIGHT_API_DATABASE_URL": f"pg0://lifecycle-validation-{run_id}:{port}",
        "HINDSIGHT_API_LLM_PROVIDER": "openai",
        "HINDSIGHT_API_LLM_API_KEY": "synthetic-unused-key",
        "HINDSIGHT_API_LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "HINDSIGHT_API_LLM_MODEL": "synthetic",
        "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "openai",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": "synthetic-unused-key",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL": "synthetic",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS": "32",
        "HINDSIGHT_API_RERANKER_PROVIDER": "rrf",
        "HINDSIGHT_API_SKIP_LLM_VERIFICATION": "true",
        "HINDSIGHT_API_WORKER_ENABLED": "false",
        "HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION": "false",
        "HINDSIGHT_API_AUDIT_LOG_ENABLED": "false",
        "HINDSIGHT_API_LLM_TRACE_ENABLED": "false",
        "HINDSIGHT_API_TENANT_API_KEY": "synthetic-lifecycle-auth",
        "HINDSIGHT_API_DB_POOL_MIN_SIZE": "1",
        "HINDSIGHT_API_DB_POOL_MAX_SIZE": "3",
    })
    logging.disable(logging.CRITICAL)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "backend"))
    from fastapi import FastAPI
    import httpx
    from hindsight_api import MemoryEngine
    from hindsight_api.engine.schema import fq_store_table as fq
    from app.memory.native_lifecycle import install, create_router
    install()
    memory = MemoryEngine()
    # No real embedding or content provider is involved in this storage test.
    memory.embeddings._dimension = 32
    async def initialized():
        return None
    memory.embeddings.initialize = initialized
    memory.query_analyzer.load = lambda: None
    async def embedding(**kwargs):
        return str([1.0] + [0.0] * 31)
    memory._reembed_memory_text = embedding
    async def model_embedding(*args, **kwargs):
        return [1.0] + [0.0] * 31
    memory._mental_model_embedding_vector = model_embedding
    original_llm = memory._reflect_llm_config
    class Model:
        def with_config(self, *args, **kwargs):
            return self
        async def call(self, *, messages, **kwargs):
            snap = json.loads(messages[-1]["content"])
            return SimpleNamespace(content={"decision": "allow",
                "covered_fact_ids": [f["id"] for f in snap["facts"]],
                "disposable_fact_ids": [], "keep_fact_ids": [],
                "observation_ids": [o["id"] for o in snap["observations"]]})
    assertions = []
    def check(value, label):
        if not value:
            raise AssertionError(label)
        assertions.append(label)
    try:
        await memory.initialize()
        memory._reflect_llm_config = Model()
        app = FastAPI()
        app.include_router(create_router(memory))
        bank, target = "synthetic-source", "synthetic-target"
        fact, obs = uuid.uuid4(), uuid.uuid4()
        async with memory._pool.acquire() as conn:
            for ident in (bank, target):
                await conn.execute(f"INSERT INTO {fq('banks')}(bank_id) VALUES($1)", ident)
            await conn.execute(f"INSERT INTO {fq('documents')}(id,bank_id,original_text) "
                               "VALUES('document',$1,'synthetic raw source')", bank)
            await conn.execute(f"INSERT INTO {fq('memory_units')} "
                "(id,bank_id,document_id,text,event_date,fact_type,tags,consolidated_at) "
                "VALUES($1,$2,'document','synthetic fact',now(),'world',ARRAY['course'],now())", fact, bank)
            await conn.execute(f"INSERT INTO {fq('memory_units')} "
                "(id,bank_id,text,event_date,fact_type,tags,source_memory_ids,proof_count) "
                "VALUES($1,$2,'synthetic observation with conditions',now(),'observation',"
                "ARRAY['course'],ARRAY[$3]::uuid[],1)", obs, bank, fact)
            await memory._insert_pinned_mental_model(conn, mental_model_id="model",
                bank_id=bank, name="synthetic model", source_query="synthetic source query",
                content="Synthetic reusable conditions.", embedding=None, tags=["course"],
                max_tokens=500, trigger=None)
            await conn.execute(f"INSERT INTO {fq('knowledge_pages')}(id,bank_id,kind,name) "
                               "VALUES('folder',$1,'folder','synthetic folder')", bank)
            await conn.execute(f"INSERT INTO {fq('knowledge_pages')} "
                "(id,bank_id,parent_id,kind,name,mental_model_id) "
                "VALUES('page',$1,'folder','page','synthetic page','model')", bank)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                base_url="http://synthetic.invalid",
                headers={"Authorization": "Bearer synthetic-lifecycle-auth"}) as client:
            def url(ident, operation):
                return "/v1/default/banks/" + ident + "/lifecycle/" + operation
            response = await client.post(url(bank, "prepare"), json={
                "plan_id": "plan", "document_id": "document", "tag": "course"},
                headers={"Authorization": "Bearer wrong"})
            check(response.status_code == 403, "unauthenticated denial")
            response = await client.post(url(bank, "prepare"), json={
                "plan_id": "plan", "document_id": "document", "tag": "course"})
            check(response.status_code == 200 and response.json()["allowed"], "prepare native snapshot")
            prepared = response.json()
            async with memory._pool.acquire() as conn:
                await conn.execute(f"UPDATE {fq('memory_units')} SET text='changed observation' WHERE id=$1", obs)
            response = await client.post(url(bank, "commit"),
                json={"plan_id": "plan", "fingerprint": prepared["fingerprint"]})
            check(response.status_code == 409, "changed observation blocks physical deletion")
            async with memory._pool.acquire() as conn:
                await conn.execute(f"UPDATE {fq('memory_units')} SET text='synthetic observation with conditions' "
                                   "WHERE id=$1", obs)
            receipt = {"plan_id": "plan", "fingerprint": prepared["fingerprint"]}
            # updated_at is intentionally unchanged in this synthetic SQL fixture.
            response = await client.post(url(bank, "commit"), json=receipt)
            check(response.status_code == 200 and response.json()["committed"], "native commit")
            response = await client.post(url(bank, "commit"), json=receipt)
            check(response.status_code == 200 and response.json()["committed"], "idempotent native commit")
            response = await client.post(url(bank, "status"), json=receipt)
            check(response.status_code == 200 and response.json()["committed"], "read-only receipt recovery")
            async with memory._pool.acquire() as conn:
                check(not await conn.fetchval(f"SELECT 1 FROM {fq('documents')} WHERE bank_id=$1", bank),
                      "raw document physically removed")
                check(not await conn.fetchval(f"SELECT 1 FROM {fq('memory_units')} WHERE id=$1", fact),
                      "raw fact physically removed")
                row = await conn.fetchrow(f"SELECT text,source_memory_ids FROM {fq('memory_units')} WHERE id=$1", obs)
                check(row and row["text"] == "synthetic observation with conditions"
                      and row["source_memory_ids"] == [], "native observation identity and text preserved")
                check(await conn.fetchval(f"SELECT count(*) FROM {fq('product_memory_support')} WHERE bank_id=$1", bank) >= 1,
                      "withdrawal dependencies persisted")
                check(await conn.fetchval(f"SELECT count(*) FROM {fq('knowledge_pages')} WHERE bank_id=$1", bank) == 2,
                      "compaction preserves native knowledge page and folder")
            response = await client.post(url(bank, "export"), json={})
            check(response.status_code == 200 and len(response.json()["items"]) == 1, "native bundle export")
            replacement = uuid.uuid4()
            async with memory._pool.acquire() as conn:
                await conn.execute(f"UPDATE {fq('memory_units')} SET id=$2,text='synthetic updated synthesis' "
                                   "WHERE id=$1", obs, replacement)
            response = await client.post(url(bank, "export"), json={})
            check(response.status_code == 200
                  and response.json()["items"][0]["observation"]["id"] == str(replacement),
                  "native observation replacement preserves withdrawal anchors")
            bundle = response.json()
            response = await client.post(url(target, "import"), json=bundle)
            check(response.status_code == 200 and response.json()["observations_imported"] == 1,
                  "native bundle migration with reembedding")
            await client.post(url(target, "import"), json=bundle)
            async with memory._pool.acquire() as conn:
                check(await conn.fetchval(f"SELECT count(*) FROM {fq('memory_units')} WHERE bank_id=$1", target) == 1,
                      "migration import idempotent")
                check(await conn.fetchval(f"SELECT count(*) FROM {fq('knowledge_pages')} WHERE bank_id=$1", target) == 2,
                      "knowledge page tree migrated")
                check("reusable conditions" in await conn.fetchval(
                    f"SELECT content FROM {fq('mental_models')} WHERE bank_id=$1 AND id='model'", target),
                    "mental model content migrated")
            for ident in (bank, target):
                response = await client.post(url(ident, "retract"), json={"document_id": "document"})
                check(response.status_code == 200 and response.json()["observations_retracted"] == 1,
                      "explicit withdrawal removes observation in " + ident)
                async with memory._pool.acquire() as conn:
                    check(not await conn.fetchval(f"SELECT 1 FROM {fq('memory_units')} WHERE bank_id=$1", ident),
                          "no surviving derived observation in " + ident)
                    check(not await conn.fetchval(f"SELECT 1 FROM {fq('knowledge_pages')} WHERE bank_id=$1", ident),
                          "no surviving knowledge page in " + ident)
        print(json.dumps({"passed": len(assertions), "checks": assertions,
                          "model": "synthetic", "database": "owned-isolated-pg0"}))
    finally:
        owned_pg = memory._pg0
        memory._reflect_llm_config = original_llm
        await memory.close()
        if owned_pg:
            expected = "lifecycle-validation-" + run_id
            if owned_pg.name != expected or owned_pg.port != port:
                raise RuntimeError("refusing unexpected instance cleanup")
            await asyncio.to_thread(owned_pg._get_pg0().drop)


if __name__ == "__main__":
    asyncio.run(run())

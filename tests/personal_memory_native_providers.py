"""Opt-in native Hindsight providers against a synthetic internal gateway."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "backend"))


def run(python):
    import uvicorn
    from fastapi import FastAPI
    from sqlalchemy import select
    from app.auth.models import UserRow
    from app.logging_audit.db import Database
    from app.memory.models import MemorySpaceRow, MemorySettingsRow, MemorySourceRow
    from app.memory.personal import (PersonalModels, PersonalIn, PersonalModelRevision,
    PersonalEmbeddingProfile, MemoryBankRoute, MemoryMigration)
    from app.materials.corpus import CorpusIndex, CorpusDocument
    from app.memory.proxy import create_proxy_router
    from app.memory.providers import ProviderClient
    calls = []
    with tempfile.TemporaryDirectory(prefix="personal-native-") as folder:
        os.environ["APP_ENCRYPTION_KEY"] = "synthetic-native-gateway-key"
        os.environ["MEMORY_PROVIDER_PROXY_KEY"] = "synthetic-internal-key"
        db = Database("sqlite+pysqlite:///" + (Path(folder) / "gateway.db").as_posix())
        for model in (UserRow, MemorySettingsRow, MemorySpaceRow, MemorySourceRow, PersonalModelRevision,
                      PersonalEmbeddingProfile, MemoryBankRoute, MemoryMigration, CorpusDocument, CorpusIndex):
            model.__table__.create(db.engine)
        models = PersonalModels(db)
        for owner, dims in (("alice", 1024), ("bob", 1024)):
            with db.session() as session:
                session.add(UserRow(user_id=owner, email=owner+"@example.invalid",
                    password_hash="synthetic", status="approved", role="member"))
            provider = lambda kind: {"endpoint":"https://"+kind+".example.test",
                "model":owner+"-"+kind, "api_key":"synthetic-"+owner+"-"+kind}
            models.save(owner, PersonalIn(expected_revision=0, content=provider("content"),
                embedding=provider("embedding"), dimensions=dims, rerank=provider("rerank")))
            spec = models.resolve(owner)
            with db.session() as session:
                session.add(MemorySettingsRow(owner_user_id=owner, enabled=True, revision=1))
                session.add(MemorySpaceRow(space_id=owner, owner_user_id=owner,
                    scope_key="general", bank_id=owner+"-bank", status="active", epoch=1))
                session.add(MemoryBankRoute(bank_id=owner+"-bank", owner_user_id=owner,
                    revision=1, embedding_space=spec["embedding_space"]))
        original_embed, original_rerank, original_chat = ProviderClient.embed, ProviderClient.rerank, ProviderClient.chat
        def embed(self, texts, **kwargs):
            self.guard()
            calls.append(("embedding",self.spec["owner"],self.spec["dimensions"]))
            return [[1.0]+[0.0]*(self.spec["dimensions"]-1) for _ in texts]
        def rerank(self, query, docs):
            self.guard()
            calls.append(("rerank",self.spec["owner"],self.spec["dimensions"]))
            return [(i,1.0/(i+1)) for i in range(len(docs))]
        def chat(self, messages, **options):
            self.guard()
            calls.append(("content",self.spec["owner"],self.spec["dimensions"]))
            return {"id":"synthetic-result","object":"chat.completion","created":0,
                "model":"synthetic-model","choices":[{"index":0,"message":{
                    "role":"assistant","content":'{"ok":true}'},"finish_reason":"stop"}],
                "usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}
        ProviderClient.embed, ProviderClient.rerank, ProviderClient.chat = embed, rerank, chat
        app = FastAPI()
        app.include_router(create_proxy_router(db))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,
                                             log_level="critical",access_log=False))
        thread = threading.Thread(target=server.run,daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.05)
            if not server.started:
                raise RuntimeError("synthetic gateway did not start")
            code = """
import asyncio,sys,os
sys.path.insert(0,sys.argv[2])
os.environ["HINDSIGHT_API_LLM_SEND_BANK_AS_USER"]="true"
os.environ["HINDSIGHT_API_RERANKER_SEND_BANK_AS_HEADER"]="true"
from hindsight_api.engine.embeddings import OpenAIEmbeddings
from hindsight_api.engine.cross_encoder import SiliconFlowCrossEncoder
from hindsight_api.engine.memory_engine import _current_bank_id
from hindsight_api.engine.providers.openai_compatible_llm import OpenAICompatibleLLM
from app.memory.hindsight_runtime import install_bank_attribution
install_bank_attribution()
from hindsight_api.engine.memory_engine import MemoryEngine
assert MemoryEngine.create_mental_model._personal_bank_scope
assert MemoryEngine.run_consolidation._personal_bank_scope
assert MemoryEngine.refresh_mental_model._personal_bank_scope
async def one(owner):
    token=_current_bank_id.set(owner+"-bank")
    embeddings=OpenAIEmbeddings(api_key="synthetic-internal-key",model="personal-memory",
        base_url=sys.argv[1]+"/v1",dimensions=2000,max_retries=0)
    reranker=SiliconFlowCrossEncoder(api_key="synthetic-internal-key",
        model="personal-memory",base_url=sys.argv[1]+"/v1")
    llm=OpenAICompatibleLLM(provider="openai",api_key="synthetic-internal-key",
        base_url=sys.argv[1]+"/v1",model="personal-memory")
    try:
        await embeddings.initialize()
        await reranker.initialize()
        vectors=await embeddings.encode(["synthetic document"])
        assert len(vectors[0])==2000
        scores=await reranker.predict([("synthetic","one"),("synthetic","two")])
        assert len(scores)==2 and scores[0]>scores[1]
        await llm.call([{"role":"user","content":"Synthetic test"}],max_completion_tokens=16,
            max_retries=0,skip_validation=True)
    finally:
        await llm.cleanup()
        _current_bank_id.reset(token)
async def main():
    await asyncio.gather(one("alice"),one("bob"))
asyncio.run(main())
print("NATIVE_PROVIDER_PASS")
"""
            result = subprocess.run([python,"-c",code,f"http://127.0.0.1:{port}/api/internal/memory",
                str(ROOT / "server" / "backend")],
                capture_output=True,text=True,timeout=90)
            if result.returncode or "NATIVE_PROVIDER_PASS" not in result.stdout:
                print("Native provider validation failed:",result.stderr[-1800:])
                raise RuntimeError("native Hindsight provider protocol failed")
            assert set(calls)=={("embedding","alice",1024),("embedding","bob",1024),
                                ("rerank","alice",1024),("rerank","bob",1024),
                                ("content","alice",1024),("content","bob",1024)}
            print("PASS official Hindsight content/embedding/reranker, concurrent A/B bank attribution, 1024 target dimensions, 2000 storage dimensions; suppliers synthetic.")
        finally:
            server.should_exit=True
            thread.join(timeout=10)
            ProviderClient.embed, ProviderClient.rerank, ProviderClient.chat=original_embed,original_rerank,original_chat
            db.dispose()


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--hindsight-python",required=True)
    args=parser.parse_args()
    run(args.hindsight_python)

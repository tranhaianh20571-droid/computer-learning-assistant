"""Exact pgvector corpus index contract (PostgreSQL + pgvector only)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from backend.logging_audit.db import get_database_url

ROOT = Path(__file__).resolve().parents[1]
VERSION = ROOT / "server/backend/app/alembic/versions/e5l6m7n8o9p0_corpus_pgvector.py"


def migration_module(connection):
    spec = importlib.util.spec_from_file_location("corpus_pgvector", VERSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.fixture
def engine():
    url = make_url(get_database_url())
    if url.get_backend_name() != "postgresql":
        pytest.skip("pgvector requires PostgreSQL")
    admin = create_engine(url)
    with admin.connect() as connection:
        if not connection.scalar(text(
                "SELECT count(*) FROM pg_extension WHERE extname='vector'")):
            pytest.skip("pgvector extension is not installed")
    schema = "pgvector_test_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    # Explicit tables below always belong to the owned first schema; public only
    # exposes the installed extension, never ORM create_all against production.
    isolated = create_engine(url.update_query_dict({"options": "-csearch_path=" + schema + ",public"}))
    try:
        with isolated.begin() as connection:
            connection.execute(text("""
                CREATE TABLE corpus_documents (
                  document_id varchar(64) PRIMARY KEY,
                  material_id varchar(64) NOT NULL,
                  blocks_json text NOT NULL
                )
            """))
            connection.execute(text("""
                CREATE TABLE corpus_indexes (
                  index_id varchar(64) PRIMARY KEY,
                  document_id varchar(64) NOT NULL,
                  owner_user_id varchar(64) NOT NULL,
                  space varchar(64) NOT NULL,
                  status varchar(24) NOT NULL,
                  completed integer NOT NULL DEFAULT 0,
                  attempts integer NOT NULL DEFAULT 0,
                  vectors_json text NOT NULL
                )
            """))
        yield isolated
    finally:
        isolated.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def vector(width, hot=0):
    values = [0.0] * width
    values[hot] = 1.0
    return json.dumps(values)


def test_migration_backfill_and_exact_search(engine):
    blocks = [{"id": "chunk-a", "page_no": 1, "section_id": "s/1"},
              {"id": "chunk-b", "page_no": 2, "section_id": "s/2"}]
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO corpus_documents (document_id, material_id, blocks_json)
            VALUES ('doc', 'material', :blocks)
        """), {"blocks": json.dumps(blocks)})
        connection.execute(text("""
            INSERT INTO corpus_indexes
            (index_id, document_id, owner_user_id, space, status, completed, vectors_json)
            VALUES ('index', 'doc', 'alice', 'space', 'ready', 2, :vectors)
        """), {"vectors": json.dumps([json.loads(vector(1024, 0)),
                                      json.loads(vector(1024, 1))])})
        migration_module(connection).upgrade()
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT current_schema()")).startswith("pgvector_test_")
        assert connection.scalar(text("SELECT count(*) FROM corpus_vectors")) == 2
        rows = connection.execute(text("""
            SELECT chunk_id, 1 - (embedding <=> CAST(:query AS vector)) AS similarity
              FROM corpus_vectors
             WHERE owner_user_id='alice' AND space='space' AND dimensions=1024
             ORDER BY embedding <=> CAST(:query AS vector)
        """), {"query": vector(1024, 0)}).all()
        assert [row.chunk_id for row in rows] == ["chunk-a", "chunk-b"]
        assert rows[0].similarity > rows[1].similarity
        # Different dimensions must not participate in the same distance scan.
        connection.execute(text("""
            INSERT INTO corpus_vectors
            (index_id, position, chunk_id, owner_user_id, material_id, space,
             dimensions, page_no, section_id, embedding)
            VALUES ('index', 2, 'chunk-c', 'alice', 'material', 'space',
                    768, 3, 's/3', CAST(:embedding AS vector))
        """), {"embedding": vector(768, 0)})
        filtered = connection.execute(text("""
            SELECT chunk_id FROM corpus_vectors
             WHERE owner_user_id='alice' AND space='space' AND dimensions=1024
        """)).scalars().all()
        assert "chunk-c" not in filtered
        # Native 4096 vectors are storable even though pgvector cannot index them.
        connection.execute(text("""
            INSERT INTO corpus_vectors
            (index_id, position, chunk_id, owner_user_id, material_id, space,
             dimensions, page_no, section_id, embedding)
            VALUES ('index', 3, 'chunk-d', 'alice', 'material', 'space-4096',
                    4096, 4, 's/4', CAST(:embedding AS vector))
        """), {"embedding": vector(4096, 0)})
        assert connection.scalar(text(
            "SELECT vector_dims(embedding) FROM corpus_vectors WHERE chunk_id='chunk-d'")) == 4096
        connection.execute(text("DELETE FROM corpus_indexes WHERE index_id='index'"))
        assert connection.scalar(text("SELECT count(*) FROM corpus_vectors")) == 0


@pytest.fixture
def pipeline_env(monkeypatch):
    """Explicit DDL in an owned schema, including when public has application tables."""
    from types import SimpleNamespace
    from backend.logging_audit.db import Database
    from backend.auth.models import UserRow
    from backend.memory.personal import (PersonalModels, PersonalModelRevision, PersonalEmbeddingProfile,
        MemoryBankRoute, MemoryMigration)
    from backend.memory.models import MemorySettingsRow, MemorySpaceRow, MemorySourceRow
    from backend.materials.models import MaterialRow, DocumentAttachmentRow
    from backend.materials.corpus import CorpusDocument, CorpusIndex
    url = make_url(get_database_url())
    if url.get_backend_name() != "postgresql":
        pytest.skip("pgvector requires PostgreSQL")
    admin = create_engine(url)
    with admin.connect() as connection:
        if not connection.scalar(text("SELECT count(*) FROM pg_extension WHERE extname='vector'")):
            admin.dispose()
            pytest.skip("pgvector extension is not installed")
    schema = "pgvector_pipeline_" + uuid.uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    db = Database(url.update_query_dict({"options": "-csearch_path=" + schema + ",public"})
        .render_as_string(hide_password=False))
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "synthetic-pgvector-pipeline-key")
    models = (UserRow, PersonalModelRevision, PersonalEmbeddingProfile, MemoryBankRoute,
        MemoryMigration, MemorySettingsRow, MemorySpaceRow, MemorySourceRow,
        MaterialRow, DocumentAttachmentRow, CorpusDocument, CorpusIndex)
    try:
        with db.engine.begin() as connection:
            for model in models:
                model.__table__.create(connection, checkfirst=False)
            migration_module(connection).upgrade()
            for model in models:
                assert connection.scalar(text("SELECT n.nspname FROM pg_class c JOIN pg_namespace n "
                    "ON c.relnamespace=n.oid WHERE c.oid=to_regclass(:table)"),
                    {"table": model.__tablename__}) == schema
        with db.session() as session:
            for owner in ("alice", "bob"):
                session.add(UserRow(user_id=owner, email=owner + "@example.invalid",
                    password_hash="synthetic", status="approved", role="member"))
        yield SimpleNamespace(db=db, models=PersonalModels(db))
    finally:
        db.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def prepared_pipeline(env, monkeypatch):
    from test_personal_retrieval import body, material
    from backend.memory.providers import ProviderClient
    from backend.materials.corpus import CorpusWorker
    env.models.save("alice", body())
    monkeypatch.setattr(ProviderClient, "embed", lambda self, texts: [
        [1.0] + [0.0] * (self.spec["dimensions"] - 1) for _ in texts])
    monkeypatch.setattr(ProviderClient, "rerank", lambda self, q, ds: [(i, 1) for i in range(len(ds))])
    material(env, ident="synthetic-a", text="Synthetic source alpha")
    material(env, ident="synthetic-b", text="Synthetic source beta")
    worker = CorpusWorker(env.db)
    assert worker.tick() and worker.tick()


def test_partial_sql_backfill_keeps_json_document_matches(pipeline_env, monkeypatch):
    from backend.materials.corpus import search_corpus
    env = pipeline_env
    prepared_pipeline(env, monkeypatch)
    with env.db.session() as session:
        session.execute(text("DELETE FROM corpus_vectors WHERE material_id='synthetic-b'"))
    hits, reasons = search_corpus(env.db, "alice", ["synthetic-a", "synthetic-b"], "zzzzsyntheticquery")
    assert {h["material_id"] for h in hits} == {"synthetic-a", "synthetic-b"}
    assert "embedding_unavailable" not in reasons


@pytest.mark.parametrize("corruption", ["dimensions", "position", "owner", "pending"])
def test_current_complete_index_required_for_sql_backend(pipeline_env, monkeypatch, corruption):
    from backend.materials.corpus import search_corpus
    env = pipeline_env
    prepared_pipeline(env, monkeypatch)
    updates = {"dimensions": "UPDATE corpus_vectors SET dimensions=768 WHERE material_id='synthetic-b'",
        "position": "UPDATE corpus_vectors SET position=position+10 WHERE material_id='synthetic-b'",
        "owner": "UPDATE corpus_vectors SET owner_user_id='bob' WHERE material_id='synthetic-b'",
        "pending": "UPDATE corpus_indexes SET status='pending' WHERE document_id IN "
            "(SELECT document_id FROM corpus_documents WHERE material_id='synthetic-b')"}
    with env.db.session() as session:
        session.execute(text(updates[corruption]))
    hits, _ = search_corpus(env.db, "alice", ["synthetic-a", "synthetic-b"], "zzzzsyntheticquery")
    expected = {"synthetic-a"} if corruption == "pending" else {"synthetic-a", "synthetic-b"}
    assert {h["material_id"] for h in hits} == expected


@pytest.mark.parametrize("fallback", ["forced_json", "sql_failure"])
def test_sql_failure_or_json_switch_preserves_ready_checkpoint(pipeline_env, monkeypatch, fallback):
    from sqlalchemy import event
    from backend.materials.corpus import search_corpus
    env = pipeline_env
    prepared_pipeline(env, monkeypatch)
    calls = []
    def inspect_query(conn, cursor, statement, parameters, context, executemany):
        if "<=>" in statement:
            calls.append(1)
            if fallback == "sql_failure":
                raise RuntimeError("synthetic SQL failure")
    event.listen(env.db.engine, "before_cursor_execute", inspect_query)
    if fallback == "forced_json":
        monkeypatch.setenv("CORPUS_VECTOR_BACKEND", "json")
    try:
        hits, reasons = search_corpus(env.db, "alice", ["synthetic-a", "synthetic-b"], "zzzzsyntheticquery")
        assert {h["material_id"] for h in hits} == {"synthetic-a", "synthetic-b"}
        assert bool(calls) == (fallback == "sql_failure")
        assert ("embedding_unavailable" in reasons) == (fallback == "sql_failure")
    finally:
        event.remove(env.db.engine, "before_cursor_execute", inspect_query)

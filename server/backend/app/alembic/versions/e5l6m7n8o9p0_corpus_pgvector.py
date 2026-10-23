"""Dimension profile and pgvector-backed corpus index."""
import json

from alembic import op
import sqlalchemy as sa

revision = "e5l6m7n8o9p0"
down_revision = "d4i5j6k7l8m9"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    try:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception as exc:  # noqa: BLE001 - surface an actionable deployment error
        raise RuntimeError(
            "pgvector is required; install the vector extension or ask the database administrator."
        ) from exc
    op.execute("""
        CREATE TABLE IF NOT EXISTS personal_embedding_profiles (
          owner_user_id varchar(64) PRIMARY KEY,
          revision integer NOT NULL,
          status varchar(16) NOT NULL DEFAULT 'pending',
          dimensions integer,
          request_dimensions integer,
          embedding_space varchar(64),
          memory_dimensions integer,
          memory_compatible boolean,
          attempts integer NOT NULL DEFAULT 0
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS corpus_vectors (
          index_id varchar(64) NOT NULL REFERENCES corpus_indexes(index_id) ON DELETE CASCADE,
          position integer NOT NULL,
          chunk_id varchar(64) NOT NULL,
          owner_user_id varchar(64) NOT NULL,
          material_id varchar(64) NOT NULL,
          space varchar(64) NOT NULL,
          dimensions integer NOT NULL,
          page_no integer,
          section_id varchar(200),
          embedding vector NOT NULL,
          PRIMARY KEY (index_id, position)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_corpus_vectors_scope "
               "ON corpus_vectors (owner_user_id, space, dimensions, material_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_corpus_vectors_chunk "
               "ON corpus_vectors (chunk_id)")
    if bind.dialect.name != "postgresql":
        return
    rows = bind.execute(sa.text("""
        SELECT i.index_id, i.owner_user_id, i.space, i.vectors_json,
               d.material_id, d.blocks_json
          FROM corpus_indexes i
          JOIN corpus_documents d ON d.document_id = i.document_id
         WHERE i.status = 'ready' AND i.vectors_json <> '[]'
    """)).mappings().all()
    reset = sa.text("""
        UPDATE corpus_indexes
           SET status = 'pending', completed = 0, vectors_json = '[]', attempts = 0
         WHERE index_id = :index_id
    """)
    insert = sa.text("""
        INSERT INTO corpus_vectors
        (index_id, position, chunk_id, owner_user_id, material_id, space,
         dimensions, page_no, section_id, embedding)
        VALUES (:index_id, :position, :chunk_id, :owner_user_id, :material_id, :space,
                :dimensions, :page_no, :section_id, CAST(:embedding AS vector))
        ON CONFLICT (index_id, position) DO UPDATE SET embedding = EXCLUDED.embedding
    """)
    for row in rows:
        try:
            vectors = json.loads(row["vectors_json"])
            blocks = json.loads(row["blocks_json"])
        except (TypeError, ValueError):
            bind.execute(reset, {"index_id": row["index_id"]})
            continue
        if not vectors or len(vectors) != len(blocks):
            bind.execute(reset, {"index_id": row["index_id"]})
            continue
        width = len(vectors[0]) if isinstance(vectors[0], list) else 0
        if width <= 0 or any(not isinstance(v, list) or len(v) != width for v in vectors):
            bind.execute(reset, {"index_id": row["index_id"]})
            continue
        payload = []
        for position, (vector, block) in enumerate(zip(vectors, blocks, strict=True)):
            payload.append({"index_id": row["index_id"], "position": position,
                "chunk_id": block["id"], "owner_user_id": row["owner_user_id"],
                "material_id": row["material_id"], "space": row["space"],
                "dimensions": width, "page_no": block.get("page_no"),
                "section_id": (block.get("section_id") or "")[:200],
                "embedding": json.dumps(vector)})
        if payload:
            bind.execute(insert, payload)


def downgrade():
    op.execute("DROP TABLE IF EXISTS corpus_vectors")
    op.execute("DROP TABLE IF EXISTS personal_embedding_profiles")

"""Personal model versions, bank routes and structural vector indexes."""
from alembic import op
import sqlalchemy as sa

revision = "d4i5j6k7l8m9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("text_chunks", sa.Column("private_text", sa.Text(),
                                          nullable=False, server_default=""))
    from app.memory.personal import PersonalModelRevision, MemoryBankRoute, MemoryMigration
    from app.materials.corpus import CorpusDocument, CorpusIndex
    for table in (PersonalModelRevision.__table__, MemoryBankRoute.__table__, MemoryMigration.__table__,
                  CorpusDocument.__table__, CorpusIndex.__table__):
        table.create(op.get_bind(), checkfirst=True)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM text_chunks WHERE private_text <> '')")):
        raise RuntimeError("refuse to discard private text")
    for table in ("personal_memory_migrations", "personal_memory_routes", "corpus_indexes", "corpus_documents",
                  "personal_retrieval_models"):
        if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")):
            raise RuntimeError("refuse to discard private retrieval data")
    for table in ("corpus_indexes", "corpus_documents", "personal_memory_migrations", "personal_memory_routes",
                  "personal_retrieval_models"):
        op.drop_table(table)
    op.drop_column("text_chunks", "private_text")

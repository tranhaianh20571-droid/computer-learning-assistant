"""Add immutable character cards, prepared media and next-segment bindings."""
from alembic import op
import sqlalchemy as sa

revision = "s3e4f5a6b7c8"
down_revision = "r2f3a4b5c6d7"
branch_labels = None
depends_on = None

metadata = sa.MetaData()
sa.Table("conversations", metadata, sa.Column("conversation_id", sa.String(64), primary_key=True))
sa.Table("learning_tasks", metadata, sa.Column("task_id", sa.String(64), primary_key=True))


def column(name, type_, *, primary=False, nullable=False, foreign=None, index=False):
    return sa.Column(name, type_, *([sa.ForeignKey(foreign)] if foreign else []),
        primary_key=primary, nullable=nullable, index=index)


def string(name, size=64, **options):
    return column(name, sa.String(size), **options)


card_fk = "character_cards.card_id"
TABLES = (
    sa.Table("character_cards", metadata, string("card_id", primary=True),
        string("owner_user_id", index=True), column("current_revision", sa.Integer()),
        column("published_revision", sa.Integer(), nullable=True),
        column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        column("created_at", sa.DateTime(timezone=True))),
    sa.Table("character_revisions", metadata, string("card_id", primary=True, foreign=card_fk),
        column("revision", sa.Integer(), primary=True), column("profile_json", sa.Text())),
    sa.Table("character_library", metadata, string("owner_user_id", primary=True),
        string("card_id", primary=True, foreign=card_fk)),
    sa.Table("character_assets", metadata, string("asset_id", primary=True),
        string("card_id", foreign=card_fk, index=True), string("owner_user_id"),
        column("source_revision", sa.Integer()), string("kind", 12), string("scene", 24),
        string("storage_key", 200), string("mime_type"), column("confirmed", sa.Boolean()),
        column("created_at", sa.DateTime(timezone=True))),
    sa.Table("character_bindings", metadata,
        string("conversation_id", primary=True, foreign="conversations.conversation_id"),
        string("card_id", foreign=card_fk), column("revision", sa.Integer()),
        column("effective_step_no", sa.Integer()), column("profile_json", sa.Text())),
    sa.Table("character_media_jobs", metadata,
        string("task_id", primary=True, foreign="learning_tasks.task_id"),
        string("card_id", foreign=card_fk, index=True), string("owner_user_id"),
        column("revision", sa.Integer()), string("config_id"), column("config_version", sa.Integer()),
        column("request_json", sa.Text()), string("request_hash"),
        string("provider_job_id", 200, nullable=True), column("submitted", sa.Boolean()),
        column("result_json", sa.Text())),
    sa.Table("character_segments", metadata,
        string("conversation_id", primary=True, foreign="conversations.conversation_id"),
        column("step_no", sa.Integer(), primary=True), string("card_id", foreign=card_fk),
        column("revision", sa.Integer()), column("profile_json", sa.Text()), string("speech_fingerprint")),
)


def upgrade():
    for table in TABLES:
        table.create(op.get_bind())


def downgrade():
    from sqlalchemy import select, func
    bind = op.get_bind()
    if any(bind.scalar(select(func.count()).select_from(table)) for table in TABLES):
        raise RuntimeError("character_downgrade_requires_empty_tables")
    for table in reversed(TABLES):
        table.drop(bind)

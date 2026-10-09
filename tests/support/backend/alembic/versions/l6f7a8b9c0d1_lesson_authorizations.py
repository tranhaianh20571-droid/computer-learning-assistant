"""Persist explicitly confirmed teaching lesson authorization."""
from alembic import op
import sqlalchemy as sa

revision = "l6f7a8b9c0d1"
down_revision = "k5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("lesson_authorizations",
        sa.Column("authorization_id", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("course_id", sa.String(64), sa.ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False),
        sa.Column("lesson_id", sa.String(64), sa.ForeignKey("lessons.lesson_id", ondelete="CASCADE"), nullable=False),
        sa.Column("outline_version_id", sa.String(64), nullable=False),
        sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE")),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("context_json", sa.Text(), nullable=False),
        sa.Column("content_config_id", sa.String(64)),
        sa.Column("content_config_version", sa.Integer()),
        sa.Column("tts_config_id", sa.String(64)),
        sa.Column("tts_config_version", sa.Integer()),
        sa.Column("content_root_grant_id", sa.String(64), sa.ForeignKey("disclosure_grants.grant_id")),
        sa.Column("tts_root_grant_id", sa.String(64), sa.ForeignKey("disclosure_grants.grant_id")),
        sa.Column("prewarm_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("idx_lesson_authorizations_owner", "lesson_authorizations", ["owner_user_id"])
    op.create_index("idx_lesson_authorizations_lesson", "lesson_authorizations", ["lesson_id"])


def downgrade():
    op.drop_index("idx_lesson_authorizations_lesson", table_name="lesson_authorizations")
    op.drop_index("idx_lesson_authorizations_owner", table_name="lesson_authorizations")
    op.drop_table("lesson_authorizations")

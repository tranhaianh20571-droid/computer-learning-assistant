"""Owner-scoped next-lesson first segment cache."""
from alembic import op
import sqlalchemy as sa

revision="g1a2b3c4d5e6"
down_revision="e0f1a2b3c4d5"
branch_labels=None
depends_on=None


def upgrade():
    op.create_table("prepared_first_segments",
        sa.Column("cache_id",sa.String(64),primary_key=True),
        sa.Column("owner_user_id",sa.String(64),nullable=False),
        sa.Column("course_id",sa.String(64),sa.ForeignKey("courses.course_id",ondelete="CASCADE"),nullable=False),
        sa.Column("lesson_id",sa.String(64),nullable=False),
        sa.Column("text_key",sa.String(64),nullable=False),sa.Column("audio_key",sa.String(64),nullable=False,server_default=""),
        sa.Column("status",sa.String(24),nullable=False,server_default="queued"),sa.Column("task_id",sa.String(64),nullable=False),
        sa.Column("context_json",sa.Text(),nullable=False),sa.Column("plan_json",sa.Text(),nullable=False),
        sa.Column("candidate_json",sa.Text(),nullable=False,server_default="{}"),sa.Column("audio_json",sa.Text(),nullable=False,server_default="{}"),
        sa.Column("audio_object_key",sa.String(200),nullable=False,server_default=""),sa.Column("audio_bytes",sa.BigInteger(),nullable=False,server_default="0"),
        sa.Column("consumed_conversation_id",sa.String(64)),sa.Column("error_code",sa.String(64)),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.Column("expires_at",sa.DateTime(timezone=True),nullable=False),
        sa.UniqueConstraint("owner_user_id","course_id",name="uq_first_segment_owner_course"),if_not_exists=True)


def downgrade():
    op.drop_table("prepared_first_segments")

"""Private streaming snapshots and reusable lightweight lesson plans."""
from alembic import op
import sqlalchemy as sa

revision = "e0f1a2b3c4d5"
down_revision = "d9e0f1a2b3c4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("prepared_lesson_plans",
        sa.Column("cache_key", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("course_id", sa.String(64), sa.ForeignKey("courses.course_id", ondelete="CASCADE"), nullable=False),
        sa.Column("lesson_id", sa.String(64), nullable=False), sa.Column("plan_json", sa.Text(), nullable=False),
        sa.Column("source", sa.String(24), nullable=False, server_default="lightweight"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), if_not_exists=True)
    op.create_table("teaching_content_previews",
        sa.Column("task_id", sa.String(64), sa.ForeignKey("learning_tasks.task_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=False), sa.Column("task_generation", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), if_not_exists=True)


def downgrade():
    op.drop_table("teaching_content_previews")
    op.drop_table("prepared_lesson_plans")

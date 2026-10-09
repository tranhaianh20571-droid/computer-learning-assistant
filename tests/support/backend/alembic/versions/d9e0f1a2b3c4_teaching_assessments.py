"""Version-bound teaching plans and private unit assessments."""
from alembic import op
import sqlalchemy as sa

revision = "d9e0f1a2b3c4"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("teaching_steps")}
    if "phase" not in columns:
        op.add_column("teaching_steps", sa.Column("phase", sa.String(24), nullable=False, server_default=""))
    if "covered_goal_ids_json" not in columns:
        op.add_column("teaching_steps", sa.Column("covered_goal_ids_json", sa.Text(), nullable=False, server_default="[]"))
    if "lesson_finished" not in columns:
        op.add_column("teaching_steps", sa.Column("lesson_finished", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table("lesson_blueprints",
        sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("owner_user_id", sa.String(64), nullable=False), sa.Column("lesson_id", sa.String(64), nullable=False),
        sa.Column("outline_version_id", sa.String(64), nullable=False), sa.Column("content_version_id", sa.String(64), nullable=False),
        sa.Column("plan_json", sa.Text(), nullable=False), sa.Column("context_json", sa.Text(), nullable=False),
        sa.Column("source_task_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), if_not_exists=True)
    op.create_table("unit_assessments",
        sa.Column("assessment_id", sa.String(64), primary_key=True), sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("course_id", sa.String(64), nullable=False), sa.Column("unit_id", sa.String(64), nullable=False),
        sa.Column("outline_version_id", sa.String(64), nullable=False), sa.Column("task_id", sa.String(64), nullable=False, unique=True),
        sa.Column("context_json", sa.Text(), nullable=False), sa.Column("questions_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), if_not_exists=True)
    op.execute("CREATE INDEX IF NOT EXISTS idx_unit_assessments_owner_unit ON unit_assessments (owner_user_id,unit_id)")
    op.create_table("assessment_attempts",
        sa.Column("attempt_id", sa.String(64), primary_key=True),
        sa.Column("assessment_id", sa.String(64), sa.ForeignKey("unit_assessments.assessment_id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", sa.String(64), nullable=False), sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False, unique=True),
        sa.Column("answers_json", sa.Text(), nullable=False), sa.Column("feedback_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("owner_user_id", "assessment_id", "idempotency_key", name="uq_assessment_attempt_idem"), if_not_exists=True)


def downgrade():
    op.drop_table("assessment_attempts"); op.drop_table("unit_assessments"); op.drop_table("lesson_blueprints")
    for name in ("lesson_finished", "covered_goal_ids_json", "phase"): op.drop_column("teaching_steps", name)

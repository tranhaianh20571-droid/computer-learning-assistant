"""Allow a conversation ID plus a full refinement plan hash in task subjects."""
from alembic import op
import sqlalchemy as sa

revision = "j4d5e6f7a8b9"
down_revision = "i3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("learning_tasks", "task_states", "task_events"):
        op.alter_column(table, "subject_id", existing_type=sa.String(64),
            type_=sa.String(129), existing_nullable=False)


def downgrade():
    # PostgreSQL rejects narrowing when long subjects exist; never truncate IDs.
    for table in ("learning_tasks", "task_states", "task_events"):
        op.alter_column(table, "subject_id", existing_type=sa.String(129),
            type_=sa.String(64), existing_nullable=False)

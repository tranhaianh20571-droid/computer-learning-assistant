"""Keep first-segment caches isolated by lesson and immutable configuration keys."""
from alembic import op
import sqlalchemy as sa

revision = "m7a8b9c0d1e2"
down_revision = "l6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("prepared_first_segments") as batch:
        batch.drop_constraint("uq_first_segment_owner_course", type_="unique")
        batch.create_unique_constraint("uq_first_segment_owner_lesson_keys",
            ["owner_user_id", "course_id", "lesson_id", "text_key", "audio_key"])


def downgrade():
    connection = op.get_bind()
    duplicate = connection.execute(sa.text("SELECT 1 FROM prepared_first_segments "
        "GROUP BY owner_user_id, course_id HAVING COUNT(*) > 1 LIMIT 1")).first()
    if duplicate:
        raise RuntimeError("cannot downgrade lesson caches without discarding retained lesson versions")
    with op.batch_alter_table("prepared_first_segments") as batch:
        batch.drop_constraint("uq_first_segment_owner_lesson_keys", type_="unique")
        batch.create_unique_constraint("uq_first_segment_owner_course", ["owner_user_id", "course_id"])

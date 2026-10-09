"""Course library snapshots, explicit detach and revision-scoped confirmations."""

from alembic import op
import sqlalchemy as sa

revision = "b6c8d0e2f4a6"
down_revision = "f4a5b6c7d8e9"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("learning_routes", sa.Column("input_ref", sa.Text(), nullable=False, server_default=""))
    for table in ("route_versions", "outline_versions"):
        op.add_column(table, sa.Column("title_snapshot", sa.String(200), nullable=False, server_default=""))
        op.add_column(table, sa.Column("content_snapshot_json", sa.Text(), nullable=True))
        op.add_column(table, sa.Column("content_fingerprint", sa.String(80), nullable=True))
    op.add_column("route_course_nodes", sa.Column("outline_version_id", sa.String(64), nullable=True))
    op.create_table("planning_confirmations",
        sa.Column("confirmation_id", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("draft_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("draft_id", "revision", name="uq_planning_confirmation_revision"))
    op.create_table("route_detached_courses",
        sa.Column("route_id", sa.String(64), primary_key=True),
        sa.Column("course_id", sa.String(64), primary_key=True),
        sa.Column("detached_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.execute("UPDATE route_versions SET title_snapshot = r.title_ref FROM learning_routes r WHERE r.route_id = route_versions.route_id")
    op.execute("UPDATE outline_versions SET title_snapshot = c.title_ref FROM courses c WHERE c.course_id = outline_versions.course_id")
    op.execute("""UPDATE route_course_nodes n SET outline_version_id = (
        SELECT o.outline_version_id FROM outline_versions o
        WHERE o.course_id = n.course_id AND o.source_route_version_id = n.route_version_id
        ORDER BY o.version_no ASC LIMIT 1)""")


def downgrade():
    op.drop_table("route_detached_courses")
    op.drop_table("planning_confirmations")
    op.drop_column("route_course_nodes", "outline_version_id")
    for table in ("route_versions", "outline_versions"):
        for column in ("content_fingerprint", "content_snapshot_json", "title_snapshot"):
            op.drop_column(table, column)
    op.drop_column("learning_routes", "input_ref")

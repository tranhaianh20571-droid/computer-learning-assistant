"""Planning dialogue and private cover assets."""
from alembic import op, context
import sqlalchemy as sa

revision = "c8d9e0f1a2b3"
down_revision = "b6c8d0e2f4a6"
branch_labels = None
depends_on = None


def upgrade():
    if not context.is_offline_mode():
        inspector = sa.inspect(op.get_bind())
        expected = {
            "planning_dialogue_turns": {"task_id","draft_id","owner_user_id","base_revision","user_content","selected_node","base_json","result_json","messages_json","decision","created_at"},
            "planning_cover_states": {"target_kind","target_id","owner_user_id","latest_task_id","current_asset_id"},
            "planning_cover_assets": {"asset_id","task_id","target_kind","target_id","owner_user_id","method","recipe_json","image_bytes","created_at"},
        }
        present = [name for name in expected if inspector.has_table(name)]
        if present:
            if len(present) != len(expected):
                raise RuntimeError("partial planning feature schema")
            for name, columns in expected.items():
                if {c["name"] for c in inspector.get_columns(name)} != columns:
                    raise RuntimeError("planning feature schema mismatch")
            return
    op.create_table("planning_dialogue_turns",
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column("draft_id", sa.String(64), nullable=False),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("user_content", sa.Text(), nullable=False),
        sa.Column("selected_node", sa.String(80), nullable=False, server_default=""),
        sa.Column("base_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("messages_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("decision", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("idx_planning_dialogue_draft", "planning_dialogue_turns", ["draft_id", "created_at"])
    op.create_table("planning_cover_states",
        sa.Column("target_kind", sa.String(20), primary_key=True),
        sa.Column("target_id", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("latest_task_id", sa.String(64)), sa.Column("current_asset_id", sa.String(64)))
    op.create_table("planning_cover_assets",
        sa.Column("asset_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False, unique=True),
        sa.Column("target_kind", sa.String(20), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("method", sa.String(20), nullable=False, server_default="procedural"),
        sa.Column("recipe_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("image_bytes", sa.LargeBinary()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))
    op.create_index("idx_planning_cover_target", "planning_cover_assets", ["target_kind", "target_id"])


def downgrade():
    op.drop_table("planning_cover_assets")
    op.drop_table("planning_cover_states")
    op.drop_table("planning_dialogue_turns")

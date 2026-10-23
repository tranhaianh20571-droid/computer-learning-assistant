"""Personal voice choices and archived classroom playback revisions."""
from alembic import op
import sqlalchemy as sa

revision = "w7b8c9d0e1f2"
down_revision = "v6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("character_voice_choices",
        sa.Column("owner_user_id", sa.String(64), primary_key=True),
        sa.Column("card_id", sa.String(64), sa.ForeignKey("character_cards.card_id"), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("voice_id", sa.String(200), nullable=False),
        sa.Column("config_id", sa.String(64), nullable=False))
    op.create_table("classroom_playback_revisions",
        sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("effective_step_no", sa.Integer(), nullable=False),
        sa.Column("profile_json", sa.Text(), nullable=False, server_default="{}"))
    op.create_table("classroom_segment_history",
        sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.conversation_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("step_no", sa.Integer(), primary_key=True),
        sa.Column("snapshot_json", sa.Text(), nullable=False))


def downgrade():
    connection = op.get_bind()
    names = ("classroom_segment_history", "classroom_playback_revisions", "character_voice_choices")
    if any(connection.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {name})")) for name in names):
        raise RuntimeError("refusing nonempty mentor playback downgrade")
    for name in names:
        op.drop_table(name)

"""Role model bindings, complete persona assets and durable Nuwa stages."""
from alembic import op
import sqlalchemy as sa

revision="z0e1f2a3b4c5"
down_revision="y9d0e1f2a3b4"
branch_labels=None
depends_on=None


def upgrade():
    op.create_table("model_role_bindings",
        sa.Column("owner_user_id",sa.String(64),primary_key=True),
        sa.Column("role",sa.String(24),primary_key=True),
        sa.Column("config_id",sa.String(64)),
        sa.Column("revision",sa.Integer(),nullable=False,server_default="0"))
    op.create_table("task_model_roles",
        sa.Column("task_id",sa.String(64),sa.ForeignKey("learning_tasks.task_id",ondelete="CASCADE"),primary_key=True),
        sa.Column("role",sa.String(24),primary_key=True),
        sa.Column("config_id",sa.String(64),nullable=False),
        sa.Column("config_version",sa.Integer(),nullable=False),
        sa.Column("grant_id",sa.String(64)))
    op.create_table("character_personas",
        sa.Column("card_id",sa.String(64),primary_key=True),
        sa.Column("revision",sa.Integer(),primary_key=True),
        sa.Column("payload_json",sa.Text(),nullable=False),
        sa.ForeignKeyConstraint(["card_id","revision"],["character_revisions.card_id","character_revisions.revision"],ondelete="CASCADE"))
    op.create_table("character_builds",
        sa.Column("build_id",sa.String(64),primary_key=True),
        sa.Column("owner_user_id",sa.String(64),nullable=False,index=True),
        sa.Column("task_id",sa.String(64),sa.ForeignKey("learning_tasks.task_id",ondelete="CASCADE"),nullable=False),
        sa.Column("card_id",sa.String(64),nullable=False),
        sa.Column("card_revision",sa.Integer(),nullable=False),
        sa.Column("request_key",sa.String(64),nullable=False),
        sa.Column("request_json",sa.Text(),nullable=False),
        sa.Column("request_hash",sa.String(64),nullable=False),
        sa.Column("config_id",sa.String(64),nullable=False),
        sa.Column("config_version",sa.Integer(),nullable=False),
        sa.Column("sources_json",sa.Text(),nullable=False,server_default="[]"),
        sa.Column("search_started",sa.Boolean(),nullable=False,server_default=sa.false()),
        sa.Column("result_json",sa.Text(),nullable=False,server_default="{}"),
        sa.Column("accepted_revision",sa.Integer()),
        sa.Column("created_at",sa.DateTime(timezone=True),nullable=False,server_default=sa.func.now()),
        sa.UniqueConstraint("owner_user_id","request_key",name="uq_character_build_key"))
    op.create_table("character_build_stages",
        sa.Column("task_id",sa.String(64),sa.ForeignKey("learning_tasks.task_id",ondelete="CASCADE"),primary_key=True),
        sa.Column("build_id",sa.String(64),sa.ForeignKey("character_builds.build_id",ondelete="CASCADE"),nullable=False,index=True),
        sa.Column("stage_key",sa.String(64),nullable=False),
        sa.Column("input_hash",sa.String(64),nullable=False),
        sa.Column("result_json",sa.Text(),nullable=False,server_default="{}"),
        sa.UniqueConstraint("build_id","stage_key",name="uq_character_build_stage"))
    op.execute(sa.text("""INSERT INTO model_role_bindings(owner_user_id,role,config_id,revision)
        SELECT chosen.owner_user_id, roles.role, chosen.config_id, 1
        FROM (SELECT DISTINCT ON (owner_user_id) owner_user_id,config_id
              FROM service_configs WHERE kind='content_model' AND is_active=true
              AND owner_scope='user' ORDER BY owner_user_id,created_at DESC,config_id DESC) chosen
        CROSS JOIN (VALUES ('planning'),('teaching')) roles(role)"""))


def downgrade():
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM model_role_bindings WHERE role='research' OR revision>1)")):
        raise RuntimeError("refusing nonempty authored model role downgrade")
    for name in ("character_build_stages","character_builds","character_personas","task_model_roles"):
        if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {name})")):
            raise RuntimeError("refusing nonempty persona workflow downgrade")
    for name in ("character_build_stages","character_builds","character_personas","task_model_roles","model_role_bindings"):
        op.drop_table(name)

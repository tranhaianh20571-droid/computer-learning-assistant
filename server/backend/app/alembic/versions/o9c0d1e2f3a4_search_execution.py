"""Persist body-free search slots and retire legacy waits without execution."""
from alembic import op
import hashlib
import uuid
import sqlalchemy as sa

revision = "o9c0d1e2f3a4"
down_revision = "n8b9c0d1e2f3"
branch_labels = None
depends_on = None


def _converge_legacy_waits():
    bind = op.get_bind()
    # Lock both lifecycle rows; a search marker cannot authorize a newer generation.
    rows = bind.execute(sa.text("""
        SELECT lt.task_id, lt.stage, lt.generation, lt.attempt, lt.cancel_requested,
               lt.deadline_at, ts.owner_user_id, ts.subject_id, ts.progress_current,
               ts.progress_total, ts.revision, pts.abandoned_at,
               CURRENT_TIMESTAMP AS now
        FROM learning_tasks lt JOIN task_states ts ON ts.task_id = lt.task_id
        LEFT JOIN planning_task_states pts ON pts.task_id = lt.task_id
        WHERE lt.status = 'partial' AND ts.status = 'partial'
          AND lt.generation = ts.generation AND lt.attempt = ts.attempt
          AND lt.owner_user_id = ts.owner_user_id AND lt.subject_id = ts.subject_id
          AND (pts.task_id IS NULL OR pts.owner_user_id = lt.owner_user_id)
          AND (pts.wait_state IS NULL OR pts.wait_state = 'awaiting_search_decision')
          AND (ts.error_code = 'AWAITING_SEARCH_DECISION'
               OR pts.wait_state = 'awaiting_search_decision')
        ORDER BY lt.task_id FOR UPDATE OF lt, ts
    """)).mappings().all()
    for row in rows:
        if row["cancel_requested"] or row["abandoned_at"] is not None:
            status, code = "cancelled", "TASK_CANCELLED"
        elif row["deadline_at"] is not None and row["deadline_at"] <= row["now"]:
            status, code = "expired", "TASK_EXPIRED"
        else:
            status, code = "failed", "SEARCH_INTERRUPTED"
        values = dict(row, status=status, code=code,
            event_status="cancelled" if status == "cancelled" else "failed",
            event_id="evt_" + uuid.uuid4().hex, audit_id="aud_" + uuid.uuid4().hex)
        bind.execute(sa.text("""
            UPDATE learning_tasks SET status=:status, revision=revision+1,
                updated_at=:now, lease_owner=NULL, lease_token=NULL, lease_until=NULL
            WHERE task_id=:task_id
        """), values)
        bind.execute(sa.text("""
            UPDATE task_attempts SET finished_at=:now, result_status=:status
            WHERE task_id=:task_id AND finished_at IS NULL
        """), values)
        bind.execute(sa.text("""
            UPDATE task_states SET stage=:stage, status=:event_status, error_code=:code,
                revision=revision+1 WHERE task_id=:task_id
        """), values)
        # Clearing the obsolete wait is not a new learner decision or a queued retry.
        bind.execute(sa.text("""
            UPDATE planning_task_states SET wait_state=NULL, updated_at=:now
            WHERE task_id=:task_id AND wait_state='awaiting_search_decision'
        """), values)
        bind.execute(sa.text("""
            INSERT INTO task_events (event_id,task_id,owner_user_id,subject_id,sequence,
                stage,status,error_code,progress_current,progress_total,template_params,
                created_at,generation,attempt,revision)
            VALUES (:event_id,:task_id,:owner_user_id,:subject_id,
                (SELECT COALESCE(MAX(sequence),0)+1 FROM task_events WHERE task_id=CAST(:task_id AS varchar(64))),
                :stage,:event_status,:code,:progress_current,:progress_total,'{}',
                :now,:generation,:attempt,:revision+1)
        """), values)
        payload = "|".join(("search.execution", row["now"].isoformat(),
            "system:migration", "task", row["task_id"], "migration_retired", ""))
        values["content_hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        bind.execute(sa.text("""
            INSERT INTO audit_logs (audit_id,event,timestamp,actor_id,object_type,
                object_id,result,reason,role,content_hash)
            VALUES (:audit_id,'search.execution',:now,'system:migration','task',
                :task_id,'migration_retired','legacy_search_wait_converged','system',:content_hash)
        """), values)


def upgrade():
    op.create_table("search_task_budgets",
        sa.Column("task_id", sa.String(64), sa.ForeignKey("learning_tasks.task_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("logical_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("wire_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("budget_policy_version", sa.String(32), nullable=False, server_default="personal-v1"),
        sa.Column("search_window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("search_deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("logical_count >= 0 AND logical_count <= 3 AND wire_count >= 0 AND wire_count <= 3", name="ck_search_budget_counts"))
    op.create_table("search_calls",
        sa.Column("query_id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("learning_tasks.task_id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("attempt_id", sa.String(64), sa.ForeignKey("task_attempts.attempt_id", ondelete="CASCADE"), nullable=False),
        sa.Column("config_id", sa.String(64), sa.ForeignKey("service_configs.config_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("logical_slot", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="reserved"),
        sa.Column("wire_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64)),
        sa.Column("external_request_id", sa.String(36)),
        sa.Column("continuation_mode", sa.String(24)),
        sa.Column("retry_after", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("task_id", "logical_slot", name="uq_search_call_slot"),
        sa.CheckConstraint("logical_slot >= 1 AND logical_slot <= 3 AND wire_count >= 0 AND wire_count <= 3", name="ck_search_call_counts"),
        sa.CheckConstraint("state IN ('reserved','sending','succeeded','empty','failed','skipped','interrupted')", name="ck_search_call_state"))
    op.create_index("ix_search_calls_task_id", "search_calls", ["task_id"])
    _converge_legacy_waits()


def downgrade():
    # Forward retirement is intentional: never resurrect legacy configs, grants or waits.
    op.drop_index("ix_search_calls_task_id", table_name="search_calls")
    op.drop_table("search_calls")
    op.drop_table("search_task_budgets")

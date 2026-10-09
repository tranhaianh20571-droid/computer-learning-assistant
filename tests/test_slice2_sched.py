"""切片 2 T06 测试：四类 lane 调度隔离、三层并发上限、总截止。"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "support"))

from backend.logging_audit.db import get_database_url  # noqa: E402

os.environ.setdefault("LEARNING_DATABASE_URL", get_database_url())

from sqlalchemy import update  # noqa: E402

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgTaskEventStore  # noqa: E402
from backend.tasks.service import LaneCapacity, LearningTaskRow, TaskService, utcnow  # noqa: E402


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture()
def tasks(database: Database) -> TaskService:
    # 测试库卫生：把此前测试遗留的 queued/leased/running 任务置终态，
    # 避免其占用 lane 容量影响本文件的容量断言。
    from sqlalchemy import update

    with database.session() as sess:
        sess.execute(
            update(LearningTaskRow)
            .where(LearningTaskRow.status.in_(("queued", "leased", "running")))
            .values(status="succeeded", lease_token=None, lease_owner=None, lease_until=None)
        )
    return TaskService(database, PgTaskEventStore(database))


def _mk(tasks: TaskService, owner: str, lane: str, tag: str = "") -> str:
    return tasks.create_task(
        owner_user_id=owner, kind="generic", subject_id="sched",
        idempotency_key=f"sched-{uuid.uuid4().hex[:10]}-{tag}",
        stage="parse", lane=lane,
    )["task_id"]


# 非交互容量 = global(4) − interactive(1) = 3；ocr limit=2
SMALL = LaneCapacity(
    global_limit=4, interactive_limit=1,
    lane_limits={"interactive": 1, "teaching_prefetch": 2, "ocr": 2, "maintenance": 1},
    per_user_per_lane=1,
)


class TestLaneClaim:
    def test_claim_next_picks_queued_in_lane(self, tasks: TaskService) -> None:
        owner = f"user_{uuid.uuid4().hex[:6]}"
        _mk(tasks, owner, "ocr", "a")
        lease = tasks.claim_next("ocr", lease_owner="w1", capacity=SMALL)
        assert lease.task_id
        assert tasks.get_task(lease.task_id)["status"] in ("leased", "running")

    def test_lane_limit_blocks_same_lane(self, tasks: TaskService) -> None:
        # 三个用户各一条 ocr 任务：领满 ocr lane 上限（2）后第三个被拒
        for tag in ("c1", "c2", "c3"):
            _mk(tasks, f"user_{uuid.uuid4().hex[:6]}", "ocr", tag)
        tasks.claim_next("ocr", lease_owner="w1", capacity=SMALL)
        tasks.claim_next("ocr", lease_owner="w2", capacity=SMALL)
        with pytest.raises(Exception, match="capacity full"):
            tasks.claim_next("ocr", lease_owner="w3", capacity=SMALL)

    def test_ocr_saturation_keeps_interactive_reserved(
        self, tasks: TaskService, database: Database,
    ) -> None:
        """注入：OCR 占满非交互容量 → interactive 仍能领取保留槽位。"""
        # 各任务用不同用户，避免 per-user 上限先触发；
        # global=4、interactive=1 → 非交互可用 3；ocr(2)+maintenance(1) 占满
        _mk(tasks, f"user_{uuid.uuid4().hex[:6]}", "ocr", "s1")
        _mk(tasks, f"user_{uuid.uuid4().hex[:6]}", "ocr", "s2")
        _mk(tasks, f"user_{uuid.uuid4().hex[:6]}", "maintenance", "s3")
        tasks.claim_next("ocr", lease_owner="w1", capacity=SMALL)
        tasks.claim_next("ocr", lease_owner="w2", capacity=SMALL)
        tasks.claim_next("maintenance", lease_owner="w3", capacity=SMALL)
        with pytest.raises(Exception, match="capacity full"):
            tasks.claim_next("teaching_prefetch", lease_owner="w4", capacity=SMALL)
        # interactive 保留槽位不受侵占
        _mk(tasks, f"user_{uuid.uuid4().hex[:6]}", "interactive", "s5")
        lease = tasks.claim_next("interactive", lease_owner="wi", capacity=SMALL)
        assert tasks.get_task(lease.task_id)["status"] in ("leased", "running")

    def test_per_user_per_lane_cap_skips_same_user(self, tasks: TaskService) -> None:
        """同用户同 lane 达上限后，领取跳过其任务、不阻塞其他用户。"""
        cap = LaneCapacity(
            global_limit=8, interactive_limit=4,
            lane_limits={"interactive": 4, "teaching_prefetch": 3, "ocr": 2, "maintenance": 1},
            per_user_per_lane=1,
        )
        owner_a = f"user_{uuid.uuid4().hex[:6]}"
        owner_b = f"user_{uuid.uuid4().hex[:6]}"
        _mk(tasks, owner_a, "teaching_prefetch", "pa1")
        _mk(tasks, owner_b, "teaching_prefetch", "pb1")
        _mk(tasks, owner_a, "teaching_prefetch", "pa2")
        lease1 = tasks.claim_next("teaching_prefetch", lease_owner="w1", capacity=cap)
        assert tasks.get_task(lease1.task_id)["owner_user_id"] == owner_a  # 队首
        # owner_a 已达 per-user 上限 → 跳过 pa2，领取 owner_b 的任务
        lease2 = tasks.claim_next("teaching_prefetch", lease_owner="w2", capacity=cap)
        assert tasks.get_task(lease2.task_id)["owner_user_id"] == owner_b

    def test_expired_task_not_claimable(self, tasks: TaskService, database: Database) -> None:
        """队列等待计入总截止：截止已过的任务标记 expired，不再领取。"""
        owner = f"user_{uuid.uuid4().hex[:6]}"
        tid = _mk(tasks, owner, "ocr", "expired")
        with database.session() as sess:
            sess.execute(
                update(LearningTaskRow)
                .where(LearningTaskRow.task_id == tid)
                .values(deadline_at=utcnow() - timedelta(seconds=1))
            )
        with pytest.raises(Exception, match="no claimable task|capacity full"):
            tasks.claim_next("ocr", lease_owner="wexp", capacity=SMALL)
        assert tasks.get_task(tid)["status"] == "expired"


class TestRequeueForRetry:
    """用户显式重试失败/过期任务：只新 generation，不自动无限重试。"""

    def test_expired_task_recovers_and_is_claimable(self, tasks: TaskService, database: Database) -> None:
        owner = f"user_{uuid.uuid4().hex[:6]}"
        tid = _mk(tasks, owner, "ocr", "retry-expired")
        with database.session() as sess:
            sess.execute(
                update(LearningTaskRow)
                .where(LearningTaskRow.task_id == tid)
                .values(deadline_at=utcnow() - timedelta(seconds=1), status="expired")
            )
        before = tasks.get_task(tid)
        result = tasks.requeue_for_retry(tid, actor_id=owner)
        assert result["requeued"] is True
        assert result["status"] == "queued"
        after = tasks.get_task(tid)
        assert after["generation"] == before["generation"] + 1
        assert after["deadline_at"] is not None
        lease = tasks.claim_next("ocr", lease_owner="wretry", capacity=SMALL)
        assert lease.task_id == tid

    def test_queued_task_with_passed_deadline_refreshes(self, tasks: TaskService, database: Database) -> None:
        """截止已过但仍 queued：重试同样刷新截止，避免首次重试直接 TASK_EXPIRED。"""
        owner = f"user_{uuid.uuid4().hex[:6]}"
        tid = _mk(tasks, owner, "ocr", "retry-queued-stale")
        with database.session() as sess:
            sess.execute(
                update(LearningTaskRow)
                .where(LearningTaskRow.task_id == tid)
                .values(deadline_at=utcnow() - timedelta(seconds=1))
            )
        result = tasks.requeue_for_retry(tid, actor_id=owner)
        assert result["requeued"] is True
        assert tasks.get_task(tid)["status"] == "queued"

    def test_failed_task_recovers(self, tasks: TaskService, database: Database) -> None:
        owner = f"user_{uuid.uuid4().hex[:6]}"
        tid = _mk(tasks, owner, "ocr", "retry-failed")
        with database.session() as sess:
            sess.execute(
                update(LearningTaskRow)
                .where(LearningTaskRow.task_id == tid)
                .values(status="failed")
            )
        result = tasks.requeue_for_retry(tid, actor_id=owner)
        assert result["requeued"] is True and result["status"] == "queued"

    def test_succeeded_task_not_requeued(self, tasks: TaskService, database: Database) -> None:
        owner = f"user_{uuid.uuid4().hex[:6]}"
        tid = _mk(tasks, owner, "ocr", "retry-done")
        with database.session() as sess:
            sess.execute(
                update(LearningTaskRow)
                .where(LearningTaskRow.task_id == tid)
                .values(status="succeeded")
            )
        result = tasks.requeue_for_retry(tid, actor_id=owner)
        assert result["requeued"] is False and result["status"] == "succeeded"

    def test_cross_owner_rejected(self, tasks: TaskService) -> None:
        owner = f"user_{uuid.uuid4().hex[:6]}"
        tid = _mk(tasks, owner, "ocr", "retry-owner")
        with pytest.raises(Exception, match="not_owner"):
            tasks.requeue_for_retry(tid, actor_id="someone-else")

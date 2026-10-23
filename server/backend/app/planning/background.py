"""Bounded planning dispatcher; durable opt-in survives page and process exits."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread
from sqlalchemy import select, or_, and_
from sqlalchemy.orm import aliased
from ..tasks.service import LearningTaskRow, LeaseError
from ..logging_audit.models import TaskStateRow
from ..slice1.errors import Slice1Error
from .retry_policy import AUTO_RETRY_LIMIT, AUTO_RETRY_ERRORS
from .models import PlanningTaskStateRow, utcnow
from .errors import PlanningError


class PlanningBackground:
    def __init__(self, planning):
        self.planning = planning
        self.stop = Event()
        self.wake = Event()
        self.lock = Lock()
        self.thread = None
        self.pending = {}
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="planning")

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = Thread(target=self._loop, daemon=True, name="planning-dispatch")
            self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                # Database interruptions leave durable queued work for the next tick.
                pass
            self.wake.wait(1)
            self.wake.clear()

    def tick(self):
        with self.lock:
            self.pending = {k: v for k, v in self.pending.items() if not v.done()}
            available = 2 - len(self.pending)
            if not available:
                return
            with self.planning.db.session() as sess:
                newer = aliased(PlanningTaskStateRow)
                superseded = select(newer.task_id).where(
                    newer.owner_user_id == PlanningTaskStateRow.owner_user_id,
                    newer.target_id == PlanningTaskStateRow.target_id,
                    or_(newer.created_at > PlanningTaskStateRow.created_at,
                        and_(newer.created_at == PlanningTaskStateRow.created_at,
                            newer.task_id > PlanningTaskStateRow.task_id)),
                ).exists()
                ids = list(sess.scalars(select(LearningTaskRow.task_id).join(PlanningTaskStateRow,
                    PlanningTaskStateRow.task_id == LearningTaskRow.task_id).join(TaskStateRow,
                    TaskStateRow.task_id == LearningTaskRow.task_id).where(
                    PlanningTaskStateRow.background_enabled.is_(True), PlanningTaskStateRow.abandoned_at.is_(None),
                    PlanningTaskStateRow.wait_state.is_(None), LearningTaskRow.cancel_requested.is_(False),
                    ~superseded,
                    or_(
                        and_(LearningTaskRow.status.in_(["queued", "leased", "running"]),
                            or_(LearningTaskRow.status == "queued", LearningTaskRow.lease_until <= utcnow())),
                        and_(PlanningTaskStateRow.auto_retry_count < AUTO_RETRY_LIMIT,
                            LearningTaskRow.deadline_at > utcnow(),
                            or_(LearningTaskRow.status == "partial",
                                and_(LearningTaskRow.status == "failed", TaskStateRow.error_code.in_(AUTO_RETRY_ERRORS)))),
                    ),
                    ~LearningTaskRow.task_id.in_(list(self.pending)))
                    .order_by(LearningTaskRow.created_at).limit(available)))
            for ident in ids:
                self.pending[ident] = self.pool.submit(self._run, ident)

    def _run(self, ident):
        owner = self.planning.tasks.get_task(ident)["owner_user_id"]
        try:
            current = self.planning.get_planning_state(ident, actor_id=owner)
            if current["confirmed"]:
                return
            if current["task_status"] in ("failed", "partial"):
                current = self.planning.workspace.resume(ident, owner, automatic=True, dispatch=False)
                if current["task_status"] != "queued":
                    self.planning.workspace.suspend_automatic(ident, owner, None)
                    return
            self.planning.run_task(ident)
        except (PlanningError, Slice1Error) as exc:
            self.planning.workspace.suspend_automatic(ident, owner, exc.code)
        except LeaseError:
            # Another worker won the claim or the task was cancelled/expired.
            pass

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.pool.shutdown(wait=True, cancel_futures=True)

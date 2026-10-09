"""Independent bounded cover dispatcher; reads never start work."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock, Thread
from sqlalchemy import select, or_
from ..tasks.service import LearningTaskRow
from ..logging_audit.metrics import record_task_diagnostic
from .feature_models import CoverAssetRow
from .models import utcnow


class CoverBackground:
    def __init__(self, features):
        self.features = features
        self.stop = Event()
        self.wake = Event()
        self.lock = Lock()
        self.thread = None
        self.pool = None
        self.pending = {}

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.pending = {ident: future for ident, future in self.pending.items() if not future.done()}
            if self.pending:
                raise RuntimeError("Cover workers are still stopping")
            self.stop.clear()
            self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="planning-covers")
            self.thread = Thread(target=self._loop, daemon=True, name="cover-dispatch")
            self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                record_task_diagnostic(reason="worker_recovery_failed", error_code="WORKER_RECOVERY_FAILED")
            self.wake.wait(1)
            self.wake.clear()

    def tick(self):
        self.features.tasks.sweep_expired_tasks(kinds=["course_cover"])
        with self.lock:
            for ident, future in list(self.pending.items()):
                if future.done():
                    try:
                        future.result()
                    except Exception:
                        record_task_diagnostic(reason="worker_recovery_failed", error_code="WORKER_RECOVERY_FAILED", task_id=ident)
                    del self.pending[ident]
            available = 2 - len(self.pending)
            if not available or self.stop.is_set():
                return
            with self.features.db.session() as sess:
                ids = list(sess.scalars(select(LearningTaskRow.task_id).join(CoverAssetRow,
                    CoverAssetRow.task_id == LearningTaskRow.task_id).where(
                    LearningTaskRow.kind == "course_cover", LearningTaskRow.cancel_requested.is_(False),
                    LearningTaskRow.status.in_(["queued", "leased", "running"]),
                    or_(LearningTaskRow.deadline_at.is_(None), LearningTaskRow.deadline_at > utcnow()),
                    or_(LearningTaskRow.status == "queued", LearningTaskRow.lease_until.is_(None), LearningTaskRow.lease_until <= utcnow()),
                    ~LearningTaskRow.task_id.in_(list(self.pending)))
                    .order_by(LearningTaskRow.created_at, LearningTaskRow.task_id).limit(available)))
            if self.pool is None:
                self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="planning-covers")
            for ident in ids:
                self.pending[ident] = self.pool.submit(self.features._run, ident)

    def close(self):
        self.stop.set()
        self.wake.set()
        thread = self.thread
        if thread:
            thread.join(timeout=2)
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)
        self.thread = None
        self.pool = None

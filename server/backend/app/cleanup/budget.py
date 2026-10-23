"""Local soft resource budgets. No process names, paths or command lines escape."""
import shutil
from threading import Event, Lock, Thread
from time import monotonic

import psutil

GIB = 1024 ** 3
HEAVY_KINDS = ("material_parse", "source_import", "character_media")
_active = None


def current_budget():
    return _active


def disk_state(path, previous="normal", required=0):
    usage = shutil.disk_usage(path)
    free = usage.free - required
    margin = GIB if previous != "normal" else 0
    state = "normal"
    for label, absolute, fraction in (("warning", 10, .10), ("restricted", 5, .05), ("protected", 2, .02)):
        if free < max(absolute * GIB, usage.total * fraction) + margin:
            state = label
    return {"state": state, "free_bytes": usage.free, "total_bytes": usage.total}


class ResourceBudget:
    def __init__(self, storage, *, sampler=None):
        self.storage = storage
        self.sampler = sampler or self._sample
        self.stop_event = Event()
        self.thread = None
        self.lock = Lock()
        self.latest = {}
        self.updated = 0
        self.previous_disk = "normal"

    def _sample(self):
        memory = psutil.virtual_memory()
        process = psutil.Process()
        rss = 0
        for child in [process, *process.children(recursive=True)]:
            try:
                rss += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        cap = 2 if memory.total <= 8 * GIB else 3 if memory.total <= 16 * GIB else 5
        warning = 1.5 if cap == 2 else 2.5 if cap == 3 else 4
        return {"physical_bytes": memory.total, "available_bytes": memory.available,
                "rss_bytes": rss, "warning_bytes": int(warning * GIB), "limit_bytes": cap * GIB,
                "heavy_limit": 1 if memory.total <= 16 * GIB else 2,
                "memory_paused": rss >= cap * GIB or memory.available < max(GIB, memory.total * .10)}

    def snapshot(self, *, force=False):
        with self.lock:
            if force or monotonic() - self.updated >= 5 or not self.latest:
                try:
                    self.latest = self.sampler()
                    disk = disk_state(self.storage.base_dir, self.previous_disk)
                    self.previous_disk = disk["state"]
                    self.latest["disk"] = disk
                    self.latest["available"] = True
                except (OSError, psutil.Error):
                    self.latest = {"available": False, "memory_paused": True, "heavy_limit": 1}
                self.updated = monotonic()
            return dict(self.latest)

    def permits_heavy(self):
        snapshot = self.snapshot()
        return (not snapshot.get("memory_paused", True) and
                snapshot.get("disk", {}).get("state", "protected") not in ("restricted", "protected"))

    def check_write(self, size):
        from ..materials.storage import StorageError
        if size > 1024**2 and disk_state(self.storage.base_dir, required=size)["state"] == "protected":
            raise StorageError("local_storage_full")

    def start(self):
        global _active
        _active = self
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        def loop():
            while not self.stop_event.is_set():
                self.snapshot(force=True)
                self.stop_event.wait(5)
        self.thread = Thread(target=loop, name="local-memory-monitor", daemon=True)
        self.thread.start()

    def close(self):
        global _active
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        if _active is self:
            _active = None

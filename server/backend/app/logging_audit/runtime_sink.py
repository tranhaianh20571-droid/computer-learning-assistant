"""Best-effort, bounded local storage of sanitized runtime JSON events only.

No raw stdout import, content channel, SDK log, or audit-store replacement.
Files are process-separated; all writers in this interpreter share one lock.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import sys
from threading import RLock
import time
from typing import Callable, Mapping, Any

from .contracts import CONTENT_TRACE_FIELD_NAMES, LogEventType, SCHEMA_VERSION, validate_event_name
from .sanitize import sanitize_record

RETENTION_DAYS = 14
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 20
DIRECTORY_ENV = "RUNTIME_LOG_DIR"
_MANAGED_NAME = re.compile(r"^runtime-(\d{8})-p(\d+)-(\d{6,})\.jsonl$")
# Shared even for separate sink objects, including a logger and its restarted sink.
_IO_LOCK = RLock()


def default_runtime_directory() -> Path:
    """Anchor the production default to backend, independent of launch cwd."""
    configured = os.environ.get(DIRECTORY_ENV, "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parents[2] / ".runtime-logs"


class RuntimeSink:
    def __init__(self, directory: str | Path, *, max_file_bytes: int = MAX_FILE_BYTES,
                 max_files: int = MAX_FILES, retention_days: int = RETENTION_DAYS,
                 pid: int | None = None, clock: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        if min(max_file_bytes, max_files, retention_days) < 1:
            raise ValueError("runtime sink limits must be positive")
        self.directory = Path(directory)
        self.max_file_bytes, self.max_files = max_file_bytes, max_files
        self.retention_days = retention_days
        self.pid = os.getpid() if pid is None else pid
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self._last_warning: float | None = None
        self._counts = {"written": 0, "io_failures": 0, "dropped": 0,
                        "oversized": 0, "pruned": 0, "warning_failures": 0}

    def snapshot(self) -> dict[str, int]:
        """Health counters only; deliberately exclude filenames, paths and records."""
        with _IO_LOCK:
            return dict(self._counts)

    def record_io_failure(self) -> None:
        with _IO_LOCK:
            self._counts["io_failures"] += 1
            self._counts["dropped"] += 1
            self._warn()

    def _warn(self) -> None:
        """At most one fixed, registered diagnostic per 60s; no recursive sink call."""
        now = self._monotonic()
        if self._last_warning is not None and now - self._last_warning < 60:
            return
        self._last_warning = now
        record = sanitize_record({
            "schema_version": SCHEMA_VERSION,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": "WARN", "event": "observability.export.failed",
            "service": "api", "environment": "local",
            "exporter": "local_runtime", "error_code": "INTERNAL_ERROR",
            "dropped_count": self._counts["dropped"], "degraded": True,
        })
        try:
            sys.stderr.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stderr.flush()
        except (OSError, ValueError):
            self._counts["warning_failures"] += 1

    def _files(self) -> list[tuple[Path, re.Match]]:
        return [(p, match) for p in self.directory.iterdir()
                if (match := _MANAGED_NAME.fullmatch(p.name)) and p.is_file() and not p.is_symlink()]

    def _remove(self, path: Path) -> None:
        try:
            path.unlink()
            self._counts["pruned"] += 1
        except FileNotFoundError:
            pass  # Another process can prune its own closed files concurrently.

    def append(self, record: Mapping[str, Any]) -> bool:
        """Defense in depth: accept registered runtime events, never CONTENT fields.

        Caller supplies the already-sanitized record. Recheck the boundary here
        so direct use cannot make this disk output a second content channel.
        """
        spec = validate_event_name(record["event"])
        if (LogEventType.CONTENT in spec.types or LogEventType.RUNTIME not in spec.types
                or CONTENT_TRACE_FIELD_NAMES.intersection(record)):
            return False
        clean = sanitize_record(record)
        payload = (json.dumps(clean, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
        with _IO_LOCK:
            if len(payload) > self.max_file_bytes:
                self._counts["oversized"] += 1
                self._counts["dropped"] += 1
                self._warn()
                return False
            try:
                now = self._clock().astimezone(timezone.utc)
                day = now.strftime("%Y%m%d")
                cutoff = (now.date() - timedelta(days=self.retention_days - 1)).strftime("%Y%m%d")
                self.directory.mkdir(parents=True, exist_ok=True)
                files = self._files()
                for path, match in files:
                    if match[1] < cutoff:
                        self._remove(path)
                files = [(p, m) for p, m in files if m[1] >= cutoff]
                own = [(p, m) for p, m in files if m[1] == day and int(m[2]) == self.pid]
                latest = max(own, key=lambda item: int(item[1][3]), default=None)
                sequence = int(latest[1][3]) if latest else 0
                target = latest[0] if latest else self.directory / f"runtime-{day}-p{self.pid}-{sequence:06d}.jsonl"
                if latest and target.stat().st_size + len(payload) > self.max_file_bytes:
                    sequence += 1
                    target = self.directory / f"runtime-{day}-p{self.pid}-{sequence:06d}.jsonl"
                if target.is_symlink():
                    raise OSError("unsafe runtime destination")
                # Leave room for a new segment before creating it. Bound the whole
                # managed directory, including previous process IDs and dates.
                old = sorted((p for p, _ in files if p != target), key=lambda p: p.name)
                for path in old[:max(0, len(old) + 1 - self.max_files)]:
                    self._remove(path)
                with target.open("ab") as output:
                    if output.write(payload) != len(payload):
                        raise OSError("incomplete runtime write")
                self._counts["written"] += 1
                return True
            except (OSError, ValueError):
                self.record_io_failure()
                return False


@lru_cache(maxsize=8)
def _sink(directory: str, pid: int) -> RuntimeSink:
    return RuntimeSink(directory, pid=pid)


def get_runtime_sink() -> RuntimeSink:
    return _sink(str(default_runtime_directory()), os.getpid())


def runtime_sink_snapshot() -> dict[str, int]:
    return get_runtime_sink().snapshot()

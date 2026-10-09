"""Reuse persisted capability evidence and refresh recently used configs off the hot path."""
from datetime import datetime, timedelta, timezone
from threading import Event, RLock, Thread
from time import monotonic

TTL_SECONDS = 6 * 60 * 60
REFRESH_CAPABILITIES = ("text", "tool_call", "streaming")


def stamp_status(status, version, previous=None):
    now = datetime.now(timezone.utc)
    merged = {}
    for cap, result in status.items():
        result = dict(result)
        old = (previous or {}).get(cap, {})
        if result.get("state") == "unknown" and isinstance(old, dict):
            result["state"] = old.get("state", "unknown")
        result.update(checked_at=now.isoformat(), expires_at=(now+timedelta(
            seconds=300 if result.get("detail") in ("timeout", "connection_error", "rate_limited", "error", "capability_test_failed")
            else TTL_SECONDS)).isoformat(), config_version=version)
        merged[cap] = result
    return merged


class CapabilityRefresher:
    """One bounded daemon per model factory, with per-config single flight."""
    def __init__(self, configs):
        self.configs = configs
        self._lock = RLock()
        self._entries = {}
        self._stop = Event()
        self._thread = None
        import atexit
        atexit.register(self.close)

    def changed(self, config_id, owner, action):
        with self._lock:
            self._entries.pop(config_id, None)
        if action == "deactivated" or not hasattr(self,"builder"):
            return
        try:
            view = self.configs.get(config_id,actor_id=owner,is_admin=False)
            if view["kind"] == "content_model":
                self.register(view, owner, self.builder)
        except Exception:
            pass

    def register(self, view, owner, builder):
        key = view["config_id"]
        with self._lock:
            self._entries[key] = (owner, builder, monotonic())
            # Only recent configs, never a full database sweep.
            if len(self._entries) > 128:
                oldest = min(self._entries, key=lambda k: self._entries[k][2])
                self._entries.pop(oldest)
            if self._thread is None:
                self._thread = Thread(target=self._loop, name="capability-refresh", daemon=True)
                self._thread.start()

    def refresh(self, config_id, owner, builder):
        adapter = None
        try:
            view = self.configs.get(config_id, actor_id=owner, is_admin=False)
            if not view["is_active"]:
                return
            now = datetime.now(timezone.utc)
            due = []
            for cap in REFRESH_CAPABILITIES:
                result = view["capability_status"].get(cap, {})
                try:
                    expiry = datetime.fromisoformat(result.get("expires_at", ""))
                    valid = expiry > now and result.get("config_version") == view["config_version"]
                except (ValueError, TypeError, AttributeError):
                    valid = False
                if not valid:
                    due.append(cap)
            if not due:
                return
            adapter = builder(view, self.configs.resolve_credentials(config_id))
            adapter.timeout = min(adapter.timeout, 15.0)
            status = {cap: adapter.probe(cap).as_dict() for cap in due}
            self.configs.set_capability_status(config_id, stamp_status(status, view["config_version"],
                view["capability_status"]), expected_version=view["config_version"], merge=True)
        except Exception:
            # Controlled failure is observed at the HTTP layer; core teaching continues.
            pass
        finally:
            if adapter is not None:
                adapter.close()

    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                entries = list(self._entries.items())
            for key, (owner, builder, used) in entries:
                if self._stop.is_set():
                    return
                if monotonic()-used < 24*60*60:
                    self.refresh(key, owner, builder)
                else:
                    with self._lock:
                        self._entries.pop(key, None)
            self._stop.wait(60)

    def close(self):
        self._stop.set()

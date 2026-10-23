"""Persistent local storage roots and a cross-process I/O lock."""
import hashlib
import json
import os
from pathlib import Path
from threading import RLock

from filelock import FileLock

_locations = {}
_registry_lock = RLock()


class StorageLocation:
    def __init__(self, root):
        self.initial = Path(root).resolve()
        self.control = self.initial.with_name(self.initial.name + "-control")
        self.control.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(self.control / "objects.lock", timeout=120)
        self.volume_id = hashlib.sha256(str(self.control).encode()).hexdigest()[:32]
        self.observer = None
        self.write_guard = None

    def config(self):
        path = self.control / "location.json"
        return json.loads(path.read_text(encoding="utf8")) if path.is_file() else {}

    def root(self):
        return Path(self.config().get("root", str(self.initial)))

    def save(self, **values):
        with self.lock:
            data = self.config() | values
            temporary = self.control / "location.json.part"
            with temporary.open("w", encoding="utf8") as output:
                json.dump(data, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.control / "location.json")


def location_for(root):
    key = str(Path(root).resolve())
    with _registry_lock:
        if key not in _locations:
            _locations[key] = StorageLocation(root)
        return _locations[key]


def guarded(method):
    def call(self, key, *args, **kwargs):
        with self.location.lock:
            if method.__name__ in ("put", "put_file") and self.location.write_guard:
                size = len(args[0]) if method.__name__ == "put" else Path(args[0]).stat().st_size
                self.location.write_guard(size)
            result = method(self, key, *args, **kwargs)
        if self.location.observer and method.__name__ != "delete":
            self.location.observer(self, key, method.__name__ in ("get", "local_path"))
        return result
    return call

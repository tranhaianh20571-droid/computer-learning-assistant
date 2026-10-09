"""Bounded process-local HTTP pool. Credentials stay on each request."""
import atexit
from collections import OrderedDict
from contextlib import contextmanager
from threading import RLock
import httpx


class HttpClientPool:
    def __init__(self, capacity=32):
        self.capacity = capacity
        self._lock = RLock()
        self._entries = OrderedDict()

    def _trim(self):
        for key in list(self._entries):
            if len(self._entries) <= self.capacity:
                break
            client, active = self._entries[key]
            if not active:
                self._entries.pop(key)
                client.close()

    @contextmanager
    def borrow(self, key):
        with self._lock:
            if key not in self._entries:
                self._entries[key] = [httpx.Client(follow_redirects=False,
                    limits=httpx.Limits(max_connections=16, max_keepalive_connections=8)), 0]
            self._entries.move_to_end(key)
            entry = self._entries[key]
            entry[1] += 1
            self._trim()
        try:
            yield entry[0]
        finally:
            with self._lock:
                entry[1] -= 1
                self._trim()

    def close(self):
        with self._lock:
            for client, _ in self._entries.values():
                client.close()
            self._entries.clear()


HTTP_CLIENTS = HttpClientPool()
atexit.register(HTTP_CLIENTS.close)

"""Cooperative service shutdown when the owning desktop supervisor closes stdin."""
import runpy
import sys
import threading

import uvicorn

from .desktop_lifecycle import wait_for_owner


def main():
    stopped = threading.Event()
    servers = []
    original = uvicorn.Server.__init__

    def initialize(server, *args, **kwargs):
        original(server, *args, **kwargs)
        servers.append(server)
        if stopped.is_set():
            server.should_exit = True

    uvicorn.Server.__init__ = initialize

    def watch():
        wait_for_owner()
        stopped.set()
        for server in servers:
            server.should_exit = True

    threading.Thread(target=watch, daemon=True).start()
    if sys.argv[1] == "api":
        uvicorn.run("app.main:app", host="127.0.0.1", port=int(sys.argv[2]),
            access_log=False, log_level="critical", timeout_graceful_shutdown=10)
    else:
        script = sys.argv[2]
        sys.argv = [script]
        runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()

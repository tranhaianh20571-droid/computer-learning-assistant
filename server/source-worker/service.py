"""Authenticated private video adapter. Access logs deliberately omit requests."""
from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

KEY = os.environ.get("SOURCE_VIDEO_API_KEY", "")
JOBS = {}
LOCK = threading.Lock()
WORKER = Path(__file__).with_name("video_worker.py")
LIMIT = 64 * 1024 * 1024


def terminate(child):
    if child.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=10)


def execute(ident, payload):
    child_env = {key: value for key, value in os.environ.items() if key.upper() in (
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA",
        "SOURCE_ASR_MODELS_DIR")}
    with tempfile.TemporaryFile() as output:
        child = subprocess.Popen([sys.executable, str(WORKER)], stdin=subprocess.PIPE,
            stdout=output, stderr=subprocess.DEVNULL, env=child_env, start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            child.stdin.write(json.dumps(payload).encode())
            child.stdin.close()
            while child.poll() is None:
                with LOCK:
                    job = JOBS[ident]
                    cancelled = job["cancelled"] or time.monotonic() - job["created"] > 7200
                if cancelled:
                    raise TimeoutError()
                time.sleep(.5)
            output.seek(0)
            raw = output.read(LIMIT + 1)
            if len(raw) > LIMIT:
                raise ValueError()
            result = json.loads(raw)
            with LOCK:
                job = JOBS[ident]
                if result.get("ok") and not job["cancelled"]:
                    job.update(status="ready", result=result["result"])
                else:
                    job.update(status="failed")
        except Exception:
            with LOCK:
                JOBS[ident]["status"] = "failed"
        finally:
            terminate(child)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, value):
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        supplied = self.headers.get("Authorization", "")
        if not KEY or not hmac.compare_digest(supplied.encode(), ("Bearer " + KEY).encode()):
            return self.reply(401, {"code": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 100_000:
                return self.reply(413, {"code": "request_too_large"})
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/v1/health":
                import importlib.util
                models = Path(os.environ.get("SOURCE_ASR_MODELS_DIR", "/models"))
                return self.reply(200, {"connection": "available",
                    "video_dependencies": all(importlib.util.find_spec(name) is not None
                        for name in ("yt_dlp", "faster_whisper", "imageio_ffmpeg")),
                    "asr_models": [name for name in ("tiny", "base", "small", "medium", "large-v3")
                        if (models / name / "model.bin").is_file()]})
            if self.path == "/v1/extract":
                if set(body) != {"url", "options"} or not isinstance(body["url"], str) or len(body["url"]) > 8192:
                    raise ValueError()
                from urllib.parse import urlsplit
                parsed = urlsplit(body["url"])
                if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError()
                options = body["options"]
                if not isinstance(options, dict) or options.get("asr_model", "small") not in (
                        "tiny", "base", "small", "medium", "large-v3"):
                    raise ValueError()
                for name, low, high in (("interval", 1, 120), ("max_frames", 1, 600), ("grid_size", 1, 4)):
                    if name in options and (type(options[name]) is not int or not low <= options[name] <= high):
                        raise ValueError()
                with LOCK:
                    for ident in list(JOBS):
                        if JOBS[ident]["status"] != "processing" and time.monotonic() - JOBS[ident]["created"] > 3600:
                            del JOBS[ident]
                    if sum(job["status"] == "processing" for job in JOBS.values()) >= 2 or len(JOBS) >= 16:
                        return self.reply(429, {"code": "worker_busy"})
                    ident = uuid.uuid4().hex
                    JOBS[ident] = {"status": "processing", "created": time.monotonic(), "cancelled": False}
                threading.Thread(target=execute, args=(ident, body), daemon=True).start()
                return self.reply(202, {"job_id": ident})
            matched = re.fullmatch(r"/v1/jobs/([a-f0-9]{32})(/cancel)?", self.path)
            if matched:
                with LOCK:
                    job = JOBS.get(matched[1])
                    if not job:
                        return self.reply(404, {"code": "job_not_found"})
                    if matched[2]:
                        job.update(cancelled=True, status="failed")
                        job.pop("result", None)
                    value = {"status": job["status"]}
                    if "result" in job:
                        value["result"] = job["result"]
                return self.reply(200, value)
            return self.reply(404, {"code": "not_found"})
        except (ValueError, TypeError, KeyError):
            return self.reply(400, {"code": "invalid_request"})


if __name__ == "__main__":
    if len(KEY) < 32:
        raise SystemExit("SOURCE_VIDEO_API_KEY must contain at least 32 characters.")
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8092"))), Handler).serve_forever()

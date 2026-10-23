"""Bounded subprocess parsing with disk outputs, cancellation and tree reaping."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic

from filelock import FileLock, Timeout
import psutil

from ..cleanup.budget import current_budget, GIB
from ..planning.errors import error


def terminate_tree(child):
    try:
        root = psutil.Process(child.pid)
        children = root.children(recursive=True)
        for process in reversed(children):
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        root.kill()
        psutil.wait_procs([root, *children], timeout=5)
    except psutil.NoSuchProcess:
        pass
    child.wait(timeout=5)


def execute_local(operation, source, *, guard=None, stop=None, **options):
    budget = current_budget()
    # Unit/injected callers retain their established synchronous contract.
    if budget is None:
        return None
    if guard:
        guard()
    if stop and stop.is_set():
        raise error("TASK_CANCELLED")
    limit = budget.snapshot()["heavy_limit"]
    slot = None
    for index in range(limit):
        candidate = FileLock(budget.storage.location.control / f"parser-{index}.lock", timeout=0)
        try:
            candidate.acquire()
            slot = candidate
            break
        except Timeout:
            continue
    if slot is None:
        raise error("MODEL_RATE_LIMITED", "本地处理繁忙，请稍后重试。")
    try:
        if not budget.permits_heavy():
            raise error("MODEL_RATE_LIMITED", "本机资源不足，已暂停新增处理。")
        with tempfile.TemporaryDirectory(prefix="local-parse-") as directory:
            destination = Path(directory) / "result"
            request = {"operation": operation, "source": str(source), "output": str(destination), **options}
            package_root = Path(__file__).resolve().parents[2]
            env = {key: value for key, value in os.environ.items() if key.upper() in (
                "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "LOCALAPPDATA")}
            env["PYTHONPATH"] = str(package_root)
            env["LOCAL_PARSER_CHILD"] = "1"
            child = subprocess.Popen([sys.executable, "-m", "app.materials.local_worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                child.stdin.write(json.dumps(request).encode())
                child.stdin.close()
                started = monotonic()
                while child.poll() is None:
                    if guard:
                        guard()
                    if stop and stop.wait(.1):
                        raise error("TASK_CANCELLED")
                    if not stop:
                        import time
                        time.sleep(.1)
                    if monotonic()-started > 180:
                        raise error("MODEL_TIMEOUT")
                    try:
                        process = psutil.Process(child.pid)
                        rss = sum(p.memory_info().rss for p in [process, *process.children(recursive=True)])
                        if rss > min(2*GIB, budget.snapshot()["limit_bytes"]//2):
                            raise error("OCR_OUTPUT_INVALID", "本地解析超过内存预算，请拆分资料。")
                    except psutil.NoSuchProcess:
                        pass
                if child.returncode:
                    try:
                        code = json.loads(child.stdout.read(1024)).get("error_code")
                    except (ValueError, TypeError):
                        code = "OCR_OUTPUT_INVALID"
                    raise error(code if code in ("file_too_large", "empty_file", "invalid_file_signature",
                        "text_too_large") else "OCR_OUTPUT_INVALID")
                if guard:
                    guard()
                if not destination.is_file() or destination.stat().st_size > 200_000_000:
                    raise error("MODEL_RESPONSE_TOO_LARGE")
                if operation.endswith("_bytes") or operation == "image":
                    return destination.read_bytes()
                return json.loads(destination.read_text(encoding="utf8"))
            finally:
                if child.poll() is None:
                    terminate_tree(child)
                child.stdout.close()
    finally:
        slot.release()

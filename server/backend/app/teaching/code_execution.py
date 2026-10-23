"""Version-bound classroom executions in disposable, offline containers."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
import json
import os
import re
import shutil
import subprocess
from threading import Event, Lock
import time
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .errors import terr
from .interactive import guard
from .models import BoardSceneRow, TeachingStepRow
from ..planning.models import load_json_field

ALIASES = {"java": "java", "c": "c", "cpp": "cpp", "c++": "cpp", "cxx": "cpp",
           "pytorch": "pytorch", "torch": "pytorch"}


def runtime_for(language, code):
    language = language.strip().lower()
    if language in ALIASES:
        return ALIASES[language]
    if language in ("py", "python", "python3"):
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return "pytorch" if re.search(r"\btorch\b", code) else ""
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "torch" for a in node.names):
                return "pytorch"
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "torch":
                return "pytorch"
    return ""


class ExecutionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    playback_version: int = Field(ge=0)
    step_no: int = Field(ge=0, le=24)
    generation: int = Field(default=1, ge=1)
    block_id: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    original_code: str = Field(max_length=1200)
    code: str = Field(min_length=1, max_length=12000)


def execution_language(service, ident, owner, body):
    from ..characters.classroom import playback_version
    with service.db.session() as sess:
        conv, _ = guard(service, sess, ident, owner)
        if body.playback_version != playback_version(sess, ident) or body.generation > conv.generation:
            raise terr("CHECKPOINT_STALE", status=409)
        if body.step_no == 0:
            scene = sess.scalar(select(BoardSceneRow).where(BoardSceneRow.conversation_id == ident,
                BoardSceneRow.step_no == 0, BoardSceneRow.generation == body.generation)
                .order_by(BoardSceneRow.scene_version_no.desc()))
        else:
            step = sess.scalar(select(TeachingStepRow).where(TeachingStepRow.conversation_id == ident,
                TeachingStepRow.step_no == body.step_no, TeachingStepRow.generation == body.generation))
            scene = sess.get(BoardSceneRow, step.board_scene_id) if step else None
        blocks = load_json_field(scene.scene_json, {}).get("presentation", {}).get("blocks", []) if scene else []
        block = next((b for b in blocks if b.get("id") == body.block_id and b.get("type") == "code"), None)
        if not block or block.get("code") != body.original_code:
            raise terr("CHECKPOINT_STALE", status=409)
        language = runtime_for(block.get("language", ""), body.code)
        if not language:
            raise terr("invalid_field", "这段代码使用浏览器运行环境。", status=400)
        return language


@dataclass
class Run:
    owner: str
    conversation: str
    name: str
    cancelled: Event = field(default_factory=Event)
    finished: bool = False
    cleanup_pending: bool = False
    timestamp: float = field(default_factory=time.monotonic)


class ContainerRunner:
    def __init__(self, command=None, image=None):
        # Deployment inputs only; request fields never enter the command prefix.
        configured = os.environ.get("CLASSROOM_DOCKER_COMMAND_JSON")
        self.command = command or (json.loads(configured) if configured else
            (["wsl.exe", "--", "docker"] if os.name == "nt" and not shutil.which("docker.exe") else ["docker"]))
        if not isinstance(self.command, list) or not self.command or any(not isinstance(v, str) for v in self.command):
            raise ValueError("invalid classroom container command")
        self.image = image or os.environ.get("CLASSROOM_RUNNER_IMAGE", "learning-classroom:1")
        self.lock = Lock()
        self.runs: dict[str, Run] = {}

    def _control(self, args, timeout=8):
        try:
            return subprocess.run([*self.command, *args], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired):
            return None

    def available(self):
        self._recover_cleanup()
        result = self._control(["image", "inspect", "--format", "{{.Os}}", self.image], timeout=5)
        return result is not None and result.returncode == 0 and result.stdout.strip() == b"linux"

    def _remove(self, name):
        result = self._control(["rm", "-f", name])
        return result is not None and (result.returncode == 0 or
            b"No such container" in (result.stderr or b""))

    def _recover_cleanup(self):
        with self.lock:
            pending = [run for run in self.runs.values() if run.cleanup_pending]
        for run in pending:
            if self._remove(run.name):
                with self.lock:
                    run.cleanup_pending = False
                    run.finished = True
                    run.timestamp = time.monotonic()

    @staticmethod
    def _key(value):
        try:
            return UUID(value).hex
        except ValueError:
            raise terr("invalid_field", status=400) from None

    def _prune(self):
        cutoff = time.monotonic() - 600
        self.runs = {k: v for k, v in self.runs.items() if not v.finished or v.timestamp > cutoff}

    def cancel(self, run_id, owner, conversation):
        key = self._key(run_id)
        self._recover_cleanup()
        with self.lock:
            self._prune()
            run = self.runs.get(key)
            if run and (run.owner != owner or run.conversation != conversation):
                raise terr("conversation_not_found", status=404)
            if not run:
                if len(self.runs) >= 4096:
                    raise terr("classroom_busy", "运行请求较多，请稍后重试。", status=429)
                run = Run(owner, conversation, "classroom-" + key, finished=True)
                self.runs[key] = run
            run.cancelled.set()
        if not run.finished and not self._remove(run.name):
            raise terr("classroom_unavailable", "停止请求已接收，运行环境回收尚未确认。", status=503)
        return {"stopped": True}

    def execute(self, run_id, owner, conversation, language, code):
        key = self._key(run_id)
        self._recover_cleanup()
        with self.lock:
            self._prune()
            previous = self.runs.get(key)
            if previous:
                if previous.owner != owner or previous.conversation != conversation:
                    raise terr("conversation_not_found", status=404)
                if previous.cancelled.is_set():
                    return {"status": "stopped", "output": "运行已停止。", "images": []}
                raise terr("CHECKPOINT_STALE", "本次运行已提交，请显式重新运行。", status=409)
            active = [run for run in self.runs.values() if not run.finished]
            if len(active) >= 3 or any(run.owner == owner for run in active) or len(self.runs) >= 4096:
                raise terr("classroom_busy", "运行环境忙，请稍后重试。", status=429)
            run = Run(owner, conversation, "classroom-" + key)
            self.runs[key] = run
        proc = None
        deadline = time.monotonic() + 45
        try:
            result = self._control(["create", "--name", run.name, "--network", "none",
                "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--user", "65534:65534", "--pids-limit", "128", "--memory", "1g",
                "--memory-swap", "1g", "--cpus", "1", "--ulimit", "nofile=128:128",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m,mode=1777",
                "--tmpfs", "/work:rw,exec,nosuid,nodev,size=64m,mode=1777",
                "--log-driver", "none", "--pull", "never", "--interactive", self.image])
            if run.cancelled.is_set():
                return {"status": "stopped", "output": "运行已停止。", "images": []}
            if not result or result.returncode:
                raise terr("classroom_unavailable", "隔离运行环境不可用，请稍后重试。", status=503)
            proc = subprocess.Popen([*self.command, "start", "--attach", "--interactive", run.name],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            # Container output is bounded independently below, including forged supervisor output.
            from threading import Thread
            received = bytearray()
            overflow = Event()
            def read():
                while True:
                    part = proc.stdout.read(65536)
                    if not part:
                        break
                    if len(received) + len(part) > 8_100_000:
                        overflow.set()
                        break
                    received.extend(part)
            reader = Thread(target=read, daemon=True)
            reader.start()
            proc.stdin.write(json.dumps({"language": language, "code": code}).encode())
            proc.stdin.close()
            while proc.poll() is None:
                if run.cancelled.wait(.1) or overflow.is_set() or time.monotonic() >= deadline:
                    self._control(["rm", "-f", run.name])
                    proc.kill()
                    proc.wait(timeout=5)
                    reader.join(timeout=5)
                    return {"status": "stopped", "output": "运行已停止或超时，请重新运行。", "images": []}
            reader.join(timeout=5)
            if run.cancelled.is_set():
                return {"status": "stopped", "output": "运行已停止。", "images": []}
            try:
                value = json.loads(received)
            except (ValueError, UnicodeDecodeError):
                return {"status": "error", "output": "运行未完成，可能超出内存或资源限制。", "images": []}
            if not isinstance(value, dict):
                return {"status": "error", "output": "运行返回无效结果。", "images": []}
            images = value.get("images", [])
            return {"status": value.get("status") if value.get("status") in ("success", "error", "stopped") else "error",
                "output": str(value.get("output", ""))[:32768],
                "images": [v for v in images[:4] if isinstance(v, str) and len(v) < 2_000_000
                    and re.fullmatch(r"data:image/png;base64,[A-Za-z0-9+/=]+", v)] if isinstance(images, list) else []}
        except (OSError, subprocess.SubprocessError):
            raise terr("classroom_unavailable", "隔离运行环境不可用，请稍后重试。", status=503) from None
        finally:
            removed = self._remove(run.name)
            if proc:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc.stdout:
                    proc.stdout.close()
            with self.lock:
                run.finished = removed
                run.cleanup_pending = not removed
                run.timestamp = time.monotonic()
            if not removed:
                raise terr("classroom_unavailable", "运行环境回收尚未确认，请稍后重试。", status=503)


def create_execution_router(service, resolve_user):
    router = APIRouter()
    if os.environ.get("STUDY_DESKTOP_MODE") == "1":
        from .native_execution import NativeRunner
        runner = NativeRunner()
    else:
        runner = ContainerRunner()

    def check(ident, request):
        owner = resolve_user(request).user_id
        with service.db.session() as sess:
            guard(service, sess, ident, owner)
        return owner

    @router.get("/{ident}/interactive/runtimes")
    def runtimes(ident: str, request: Request):
        check(ident, request)
        return JSONResponse({"container": runner.available(), "languages": ["java", "c", "cpp", "pytorch"],
                             "device": "cpu"}, headers={"Cache-Control": "no-store"})

    @router.post("/{ident}/interactive/runs/{run_id}")
    def execute(ident: str, run_id: str, body: ExecutionIn, request: Request):
        owner = resolve_user(request).user_id
        language = execution_language(service, ident, owner, body)
        value = runner.execute(run_id, owner, ident, language, body.code)
        execution_language(service, ident, owner, body)
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @router.delete("/{ident}/interactive/runs/{run_id}")
    def cancel(ident: str, run_id: str, request: Request):
        return JSONResponse(runner.cancel(run_id, check(ident, request), ident),
                            headers={"Cache-Control": "no-store"})

    return router

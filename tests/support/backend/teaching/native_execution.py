"""Windows AppContainer executor; never falls back to running user code directly."""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from .code_execution import ContainerRunner, Run
from .errors import terr


class NativeRunner(ContainerRunner):
    def __init__(self, root=None, work=None):
        super().__init__(command=["native-desktop"])
        self.root = Path(root or os.environ["CLASSROOM_NATIVE_ROOT"]).resolve()
        self.work = Path(work or os.environ["CLASSROOM_NATIVE_WORK"]).resolve()
        self.helper = self.root / "sandbox" / "Sandbox.exe"
        self.processes = {}

    def available(self):
        return os.name == "nt" and all(file.is_file() for file in (
            self.helper, self.root / "python.exe", self.root / "jdk/bin/java.exe",
            self.root / "mingw/bin/clang-23.exe"))

    def _remove(self, name):
        with self.lock:
            process = self.processes.get(name)
        if process and process.poll() is None:
            process.kill()  # Closing the helper's Job Object kills the sandbox tree.
            process.wait(timeout=5)
        return True

    def invoke(self, directory, executable, args, run):
        if run.cancelled.is_set():
            return {"status": "stopped", "output": ""}
        request = {"root": str(self.root), "directory": str(directory), "owner": os.getpid(),
                   "executable": str(executable), "args": args}
        env = {key: value for key, value in os.environ.items()
               if key.upper() in ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA")}
        process = subprocess.Popen([str(self.helper)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        with self.lock:
            self.processes[run.name] = process
        if run.cancelled.is_set():
            self._remove(run.name)
            return {"status": "stopped", "output": ""}
        try:
            timeout = 75 if (self.root / ".sandbox-acl-ready").exists() else 600
            output, _ = process.communicate(json.dumps(request).encode(), timeout=timeout)
            if len(output) > 150000:
                raise ValueError()
            value = json.loads(output)
            if value.get("isolation_failed"):
                raise terr("classroom_unavailable", "隔离运行环境不可用，请重试。", status=503)
            return value
        except (ValueError, subprocess.TimeoutExpired):
            self._remove(run.name)
            return {"status": "stopped", "output": "运行已停止或超时。"}
        finally:
            with self.lock:
                self.processes.pop(run.name, None)

    def execute(self, run_id, owner, conversation, language, code):
        key = self._key(run_id)
        with self.lock:
            self._prune()
            previous = self.runs.get(key)
            if previous:
                if (previous.owner, previous.conversation) != (owner, conversation):
                    raise terr("conversation_not_found", status=404)
                if previous.cancelled.is_set():
                    return {"status": "stopped", "output": "运行已停止。", "images": []}
                raise terr("CHECKPOINT_STALE", status=409)
            active = [item for item in self.runs.values() if not item.finished]
            if len(active) >= 3 or any(item.owner == owner for item in active) or len(self.runs) >= 4096:
                raise terr("classroom_busy", status=429)
            run = Run(owner, conversation, "classroom-" + key)
            self.runs[key] = run
        try:
            if not self.available():
                raise terr("classroom_unavailable", status=503)
            self.work.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=key + "-", dir=self.work) as name:
                directory = Path(name)
                images = []
                if language in ("c", "cpp"):
                    source = directory / ("main.c" if language == "c" else "main.cpp")
                    source.write_text(code, encoding="utf8")
                    executable = directory / "program.exe"
                    value = self.invoke(directory, self.root / "mingw/bin/clang-23.exe",
                        ["--driver-mode=gcc" if language == "c" else "--driver-mode=g++",
                         "-std=c17" if language == "c" else "-std=c++17", "-O0", str(source),
                         "-o", str(executable), "-lm", "-static"], run)
                    if value["status"] == "success":
                        value = self.invoke(directory, executable, [], run)
                elif language == "java":
                    public = re.search(r"\bpublic\s+(?:(?:final|abstract)\s+)?class\s+([A-Za-z_$][\w$]*)", code)
                    first = public or re.search(r"\bclass\s+([A-Za-z_$][\w$]*)", code)
                    class_name = first.group(1) if first else "Main"
                    package = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", code)
                    target = (package.group(1) + "." if package else "") + class_name
                    source = directory / (class_name + ".java")
                    source.write_text(code, encoding="utf8")
                    value = self.invoke(directory, self.root / "jdk/bin/javac.exe",
                        ["-J-Xmx256m", "-J-Dfile.encoding=UTF-8", "-encoding", "UTF-8", "-d", str(directory), str(source)], run)
                    if value["status"] == "success":
                        value = self.invoke(directory, self.root / "jdk/bin/java.exe",
                            ["-Xmx384m", "-Dfile.encoding=UTF-8", "-XX:ActiveProcessorCount=1", "-cp", str(directory), target], run)
                elif language == "pytorch":
                    source = directory / "example.py"
                    source.write_text(code, encoding="utf8")
                    wrapper = directory / "wrapper.py"
                    wrapper.write_text(
                        "import runpy\nimport matplotlib\nmatplotlib.use('Agg')\n"
                        "import matplotlib.pyplot as plt\n"
                        "runpy.run_path('example.py',run_name='__main__')\n"
                        "for i,n in enumerate(plt.get_fignums()[:4]):\n"
                        " plt.figure(n).savefig('plot-%d.png'%i,format='png',dpi=90)\n",
                        encoding="utf8")
                    value = self.invoke(directory, self.root / "python.exe", ["-I", "-B", "-X", "utf8", "-u", str(wrapper)], run)
                    if value["status"] == "success":
                        for index in range(4):
                            image = directory / f"plot-{index}.png"
                            if image.is_file() and not image.is_symlink() and not image.is_junction() and image.stat().st_size <= 1500000:
                                payload = image.read_bytes()
                                if payload.startswith(b"\x89PNG\r\n\x1a\n"):
                                    images.append("data:image/png;base64," + base64.b64encode(payload).decode())
                else:
                    raise terr("invalid_field", status=400)
                if run.cancelled.is_set():
                    value = {"status": "stopped", "output": "运行已停止。"}
                return {"status": value["status"], "output": value.get("output", "")[:32768], "images": images}
        finally:
            self._remove(run.name)
            with self.lock:
                run.finished = True
                run.timestamp = time.monotonic()

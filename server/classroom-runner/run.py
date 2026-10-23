"""Container-only supervisor. Never execute this on the application host."""
import base64
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time

LIMIT = 32768


def command(args, deadline):
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True, cwd="/work")
    output = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                return "stopped", output.decode("utf-8", "replace") + "\nExecution timed out."
            for key, _ in selector.select(timeout=.1):
                part = os.read(key.fileobj.fileno(), 4096)
                if not part:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(part[:max(0, LIMIT-len(output))])
                if len(output) >= LIMIT:
                    return "error", output.decode("utf-8", "replace") + "\nOutput limit reached."
        status = proc.wait(timeout=max(.01, deadline-time.monotonic()))
        return ("success" if status == 0 else "error"), output.decode("utf-8", "replace")
    finally:
        selector.close()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        proc.stdout.close()


def main():
    signal.alarm(38)
    request = json.loads(sys.stdin.buffer.read(65536))
    language, code = request["language"], request["code"]
    deadline = time.monotonic() + 30
    images = []
    if language in ("c", "cpp"):
        source = "main.c" if language == "c" else "main.cpp"
        Path(source).write_text(code, encoding="utf-8")
        compiler, standard = ("gcc", "-std=c17") if language == "c" else ("g++", "-std=c++17")
        status, output = command([compiler, standard, "-O0", source, "-o", "/work/program", "-lm"], deadline)
        if status == "success":
            status, result = command(["/work/program"], deadline)
            output += result
    elif language == "java":
        # javac requires the filename to match a public top-level class.
        public = re.search(r"\bpublic\s+(?:(?:final|abstract)\s+)?class\s+([A-Za-z_$][\w$]*)", code)
        first = public or re.search(r"\bclass\s+([A-Za-z_$][\w$]*)", code)
        name = first.group(1) if first else "Main"
        package = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", code)
        target = (package.group(1) + "." if package else "") + name
        Path(name + ".java").write_text(code, encoding="utf-8")
        status, output = command(["javac", "-J-Xmx256m", "-encoding", "UTF-8", "-d", "/work", name+".java"], deadline)
        if status == "success":
            status, result = command(["java", "-Xmx384m", "-XX:ActiveProcessorCount=1",
                                       "-cp", "/work", target], deadline)
            output += result
    elif language == "pytorch":
        Path("example.py").write_text(code, encoding="utf-8")
        wrapper = """import runpy
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
runpy.run_path("/work/example.py", run_name="__main__")
for i, n in enumerate(plt.get_fignums()[:4]):
    plt.figure(n).savefig("/work/plot-%d.png" % i, format="png", dpi=90)
plt.close("all")
"""
        status, output = command(["python", "-I", "-u", "-c", wrapper], deadline)
        if status == "success":
            for index in range(4):
                path = Path(f"/work/plot-{index}.png")
                if path.is_file() and not path.is_symlink() and path.stat().st_size <= 1500000:
                    data = path.read_bytes()
                    if data.startswith(b"\x89PNG\r\n\x1a\n"):
                        images.append("data:image/png;base64," + base64.b64encode(data).decode("ascii"))
    else:
        status, output = "error", "Unsupported runtime."
    print(json.dumps({"status": status, "output": output[:LIMIT], "images": images}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps({"status": "error", "output": "Runtime could not complete this example.", "images": []}))

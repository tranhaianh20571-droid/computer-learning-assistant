"""Real AppContainer/toolchain validation with synthetic inputs, no user data."""
import argparse
import base64
import json
import socket
import socketserver
import sys
import tempfile
import threading
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server/backend"))
from app.teaching.native_execution import NativeRunner


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    runner = NativeRunner(args.root, args.work)
    if not runner.available():
        raise RuntimeError("Native component is incomplete")
    results = []
    failures = []
    def check(language, code, expected):
        try:
            value = runner.execute(str(uuid4()), "synthetic-desktop", "synthetic-class", language, code)
        except Exception as error:  # noqa: BLE001 - report every independent live check
            value = {"status": "error", "output": type(error).__name__, "images": []}
        if value["status"] != "success" or expected not in value["output"]:
            failures.append({"language": language, "status": value["status"], "output": value["output"][:600]})
        else:
            results.append(language)
        print(language, value["status"], flush=True)
        return value
    check("c", '#include <stdio.h>\nint main(){printf("%d",2*3);}', "6")
    check("cpp", '#include <iostream>\nint main(){std::cout<<2*3;}', "6")
    check("java", 'public class Example {public static void main(String[] a){System.out.println(6);}}', "6")
    check("pytorch", "import torch\nx=torch.tensor(2.,requires_grad=True)\n(x*x).backward()\nprint(x.grad.item())", "4.0")
    image = check("pytorch", "import matplotlib.pyplot as plt\nplt.plot([0,1],[0,1])\nprint('plot-ok')", "plot-ok")
    if len(image["images"]) != 1:
        failures.append({"language": "plot", "status": "missing"})
    elif not image["images"][0].startswith("data:" + "image/png;base64,") or not base64.b64decode(
        image["images"][0].split(",", 1)[1]
    ).startswith(b"\x89PNG\r\n\x1a\n"):
        failures.append({"language": "plot", "status": "invalid-image"})
    check("pytorch", """
import ctypes
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.GetFileType.argtypes = [ctypes.c_void_p]
pipes = []
for handle in range(4, 4096, 4):
    try:
        kind = kernel.GetFileType(handle)
    except OSError:
        continue
    if kind == 3:
        pipes.append(handle)
assert not pipes, pipes
print("pipe-isolation-ok")
""", "pipe-isolation-ok")
    with tempfile.TemporaryDirectory(prefix="study-private-sentinel-") as outside:
        sentinel = Path(outside) / "private.txt"
        sentinel.write_text("SYNTHETIC_PRIVATE_SENTINEL", encoding="utf8")
        check("pytorch", f"""
try:
    open({str(sentinel)!r}).read()
    print('LEAK')
except (PermissionError, OSError):
    print('private-file-blocked')
""", "private-file-blocked")
    received = []
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            received.append(self.request.recv(64).decode())
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with socket.create_connection(server.server_address) as control:
            control.sendall(b"control")
        check("pytorch", f"""
import socket
try:
    connection=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    connection.settimeout(2)
    connection.connect(('127.0.0.1',{server.server_address[1]}))
    connection.sendall(b'sandbox')
    print('NETWORK')
except OSError:
    print('network-blocked')
""", "network-blocked")
        server.shutdown()
        thread.join(timeout=5)
        if received != ["control"]:
            failures.append({"language": "network", "status": "unexpected_connection"})
    print(json.dumps({"passed": len(results) + 1, "checks": results,
        "failures": failures,
        "scope": "real Windows AppContainer, toolchains, CPU torch, plot, file and network isolation"}))
    assert not failures


if __name__ == "__main__":
    main()

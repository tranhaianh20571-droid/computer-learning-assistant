"""Explicit real Docker/compiler/CPU torch checks; synthetic code only, no database."""
from concurrent.futures import ThreadPoolExecutor
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "backend"))
from app.teaching.code_execution import ContainerRunner


def main():
    runner = ContainerRunner()
    assert runner.available(), "Build the classroom image first."
    checks = []
    def run(language, code, expected=None):
        result = runner.execute(str(uuid4()), "synthetic-live-owner", "synthetic-conversation", language, code)
        if expected is not None:
            assert result["status"] == "success", result["output"]
            assert expected in result["output"], result["output"]
        checks.append(language + ":" + result["status"])
        return result
    run("c", '#include <stdio.h>\nint main(){int x=2; printf("%d\\n",x*3);}', "6")
    run("cpp", '#include <iostream>\n#include <vector>\nint main(){std::vector<int> v{2,3};std::cout<<v[0]*v[1];}', "6")
    run("java", 'public class Example {public static void main(String[] args){int x=2;System.out.println(x*3);}}', "6")
    run("java", 'package demo; public class Example {public static void main(String[] args){System.out.println(30);}}', "30")
    run("pytorch", 'import torch\nx=torch.tensor(2.,requires_grad=True)\n(x*x).backward()\nprint(x.grad.item())\nprint(torch.cuda.is_available())', "4.0\nFalse")
    run("pytorch", """import torch
model=torch.nn.Transformer(d_model=8,nhead=2,num_encoder_layers=1,num_decoder_layers=1,
                          dim_feedforward=16,dropout=0,batch_first=True)
result=model(torch.randn(1,3,8),torch.randn(1,2,8))
result.sum().backward()
print('transformer-shape',tuple(result.shape))
""", "transformer-shape (1, 2, 8)")
    image = run("pytorch", 'import torch\nimport matplotlib.pyplot as plt\nx=torch.arange(4)\nplt.plot(x,x*x)\nprint(x.sum().item())', "6")
    assert len(image["images"]) == 1 and len(image["images"][0]) > 1000
    checks.append("real-png")
    result = run("c", "invalid syntax")
    assert result["status"] == "error" and "error:" in result["output"]
    result = run("pytorch", "while True: print('synthetic-output-'*1000)")
    assert result["status"] == "error" and len(result["output"]) <= 32768
    run("pytorch", """import os, socket
print(os.getuid())
assert not os.path.exists('/var/run/docker.sock')
assert os.environ.get('SECRET_KEY') is None
try:
    open('/opt/forbidden', 'w')
except OSError:
    print('readonly')
else:
    raise RuntimeError('writable root')
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except OSError:
    print('offline')
else:
    raise RuntimeError('network enabled')
""", "65534\nreadonly\noffline")
    checks.append("offline-readonly-nonroot-no-host-files")
    run_id = str(uuid4())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner.execute, run_id, "synthetic-live-owner", "synthetic-conversation",
                             "pytorch", "while True: pass")
        name = "classroom-" + run_id.replace("-", "")
        for _ in range(100):
            state = runner._control(["inspect", "--format", "{{.State.Running}}", name], timeout=2)
            if state and state.returncode == 0 and state.stdout.strip() == b"true":
                break
            time.sleep(.1)
        config = runner._control(["inspect", "--format", "{{json .HostConfig}}", name])
        limits = json.loads(config.stdout)
        assert limits["NetworkMode"] == "none" and limits["ReadonlyRootfs"] is True
        assert limits["Memory"] == limits["MemorySwap"] == 1073741824
        assert limits["NanoCpus"] == 1000000000 and limits["PidsLimit"] == 128
        assert limits["Binds"] in (None, []) and limits["CapDrop"] == ["ALL"]
        assert "no-new-privileges" in limits["SecurityOpt"]
        assert limits["LogConfig"]["Type"] == "none"
        checks.append("actual-container-resource-and-isolation-limits")
        runner.cancel(run_id, "synthetic-live-owner", "synthetic-conversation")
        assert future.result(timeout=15)["status"] == "stopped"
        assert runner._control(["inspect", name]).returncode != 0
        checks.append("stop-and-container-reaped")
    result = run("c", "int main(){while(1){}}")
    assert result["status"] == "stopped"
    for run_state in runner.runs.values():
        assert runner._control(["inspect", run_state.name]).returncode != 0
    checks.append("timeout-and-all-containers-reaped")
    print(json.dumps({"passed": len(checks), "checks": checks}, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Diagnose only fixed synthetic examples; never inspect user classroom code."""
import argparse
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server/backend"))
from app.teaching.native_execution import NativeRunner

parser = argparse.ArgumentParser()
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--work", type=Path, required=True)
args = parser.parse_args()
runner = NativeRunner(args.root, args.work)
examples = {
    "c": '#include <stdio.h>\nint main(){printf("ready");}',
    "cpp": '#include <iostream>\nint main(){std::cout<<"ready";}',
    "java": 'public class Main {public static void main(String[] a){System.out.println("ready");}}',
    "pytorch": 'import torch\nassert torch.tensor([1,2]).sum().item()==3\nprint("ready")',
}
for language, code in examples.items():
    try:
        result = runner.execute(str(uuid4()), "synthetic", "synthetic", language, code)
        diagnostic = result["output"].replace(str(args.root), "<runtime>").replace(str(args.work), "<work>")
        print(json.dumps({"language": language, "status": result["status"],
            "output": diagnostic[:1200]}), flush=True)
    except Exception as error:
        print(json.dumps({"language": language, "exception": type(error).__name__}), flush=True)

"""Opt the pinned Java launchers into Windows UTF-8 paths."""
import argparse
from pathlib import Path

from windows_utf8 import patch_tools

parser = argparse.ArgumentParser()
parser.add_argument("root", type=Path)
parser.add_argument("--mt", type=Path)
args = parser.parse_args()
patch_tools(args.root / "jdk/bin", ("java.exe", "javac.exe"), "Java", args.mt)
print("Pinned Java UTF-8 manifests verified: 2 commands")

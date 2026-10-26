"""Opt the pinned PostgreSQL command-line tools into Windows UTF-8 paths."""
import argparse
from pathlib import Path

from windows_utf8 import patch_tools

parser = argparse.ArgumentParser()
parser.add_argument("root", type=Path)
parser.add_argument("--mt", type=Path)
args = parser.parse_args()
patch_tools(args.root / "bin",
    ("initdb.exe", "postgres.exe", "pg_ctl.exe", "pg_dump.exe", "pg_restore.exe", "pg_isready.exe"),
    "PostgreSQL", args.mt)
print("Pinned PostgreSQL UTF-8 manifests verified: 6 commands")

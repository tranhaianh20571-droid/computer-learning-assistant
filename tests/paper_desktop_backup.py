"""Backup the explicitly selected running desktop database without printing secrets."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import psutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    install = Path(args.install).resolve()
    root = Path(__file__).resolve().parents[1]
    processes = [p for p in psutil.process_iter(["cmdline"]) if
        "app.desktop_child" in (p.info["cmdline"] or []) and "api" in p.info["cmdline"]]
    if len(processes) != 1 or not Path(processes[0].exe()).is_relative_to(install):
        raise RuntimeError("Owned desktop process mismatch")
    environment = dict(processes[0].environ())
    os.environ.update(environment)
    sys.path.insert(0, str(root / "server/backend"))
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from app.logging_audit.db import get_database_url
    url = make_url(get_database_url())
    if url.host != "127.0.0.1":
        raise RuntimeError("Not a local owned database")
    output = Path(args.out).resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((install / "resources/runtime.json").read_text(encoding="utf8"))
    components = [item for item in manifest["components"] if item["id"] == "postgres"]
    if len(components) != 1:
        raise RuntimeError("Owned backup tool missing")
    dump = (install / "userdata/runtimes" / ("postgres-" + components[0]["sha256"][:16]) / "bin/pg_dump.exe").resolve()
    if not dump.is_relative_to(install) or not dump.is_file():
        raise RuntimeError("Owned backup tool missing")
    environment["PGPASSWORD"] = url.password
    result = subprocess.run([str(dump), "--host", url.host, "--port", str(url.port),
        "--username", url.username, "--format=custom", "--file", str(output / "study.dump"), url.database],
        env=environment, capture_output=True, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("Owned database backup failed")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            counts = {table: connection.scalar(text(f'SELECT count(*) FROM "{table}"'))
                for table in ("users", "courses", "conversations", "materials")}
            counts["desktop_schema"] = connection.scalar(text("SELECT version FROM desktop_schema"))
    finally:
        engine.dispose()
    (output / "counts.json").write_text(json.dumps(counts, indent=2), encoding="utf-8")
    print(json.dumps({"backup_bytes":(output / "study.dump").stat().st_size, **counts}))


if __name__ == "__main__":
    main()

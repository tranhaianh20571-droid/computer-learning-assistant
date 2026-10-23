"""Owned Windows desktop bootstrap. Configuration arrives on stdin, never argv."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

SCHEMA_VERSION = 1
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def emit(stage, **values):
    print(json.dumps({"stage": stage, **values}), flush=True)  # noqa: T201 - private startup protocol


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def run(arguments, *, env=None, timeout=90):
    value = subprocess.run([str(x) for x in arguments], stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        creationflags=FLAGS, timeout=timeout)
    if value.returncode:
        raise RuntimeError("SERVICE_FAILED")


def configure(config, api_port, db_port, memory_port):
    home, roots, keys = Path(config["home"]), config["roots"], config["keys"]
    from urllib.parse import quote
    database = f"postgresql://study:{quote(keys['database'], safe='')}@127.0.0.1:{db_port}/study"
    memory_database = f"postgresql://study_memory:{quote(keys['memory'], safe='')}@127.0.0.1:{db_port}/study_memory"
    values = {
        "STUDY_DESKTOP_MODE": "1", "STUDY_DESKTOP_HOME": str(home),
        "DATABASE_URL": database, "LEARNING_DATABASE_URL": database,
        "PROJECT_NAME": "Study Companion", "SECRET_KEY": keys["session"],
        "FIRST_SUPERUSER": "local@example.com", "FIRST_SUPERUSER_PASSWORD": keys["session"],
        "APP_ENCRYPTION_KEY": keys["encryption"],
        "FRONTEND_HOST": f"http://127.0.0.1:{api_port}",
        "MATERIALS_STORAGE_DIR": str(home / "data" / "materials"),
        "BACKUP_STORAGE_DIR": str(home / "backups" / "materials"),
        "RUNTIME_LOG_DIR": str(home / "logs"),
        "SOURCE_VIDEO_PYTHON": str(Path(roots["video"]) / "python.exe"),
        "SOURCE_VIDEO_WORKER": str(Path(config["worker"]) / "video_worker.py"),
        "SOURCE_ASR_MODELS_DIR": roots["asr-small"],
        "CLASSROOM_NATIVE_ROOT": roots["classroom"],
        "CLASSROOM_NATIVE_WORK": str(home / "cache" / "classroom"),
        "PERSONAL_MEMORY_API_URL": f"http://127.0.0.1:{memory_port}",
        "PERSONAL_MEMORY_API_KEY": keys["memory"],
        "MEMORY_PROVIDER_PROXY_KEY": keys["proxy"],
        "CONTENT_TRACE_ENABLED": "0", "OTEL_ENABLED": "0",
        "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1",
        "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
    }
    os.environ.update(values)
    return database, memory_database


def initialize(db):
    """Version 1 is an empty-database baseline, not an Alembic history stamp."""
    from sqlalchemy import inspect, text

    from .assembler import create_app
    from .logging_audit.models import _AUDIT_IMMUTABLE_SQL, Base
    tables = inspect(db.engine).get_table_names()
    if tables and "desktop_schema" not in tables:
        raise RuntimeError("SCHEMA_NOT_OWNED")
    if "desktop_schema" in tables:
        with db.engine.connect() as connection:
            if connection.scalar(text("SELECT version FROM desktop_schema")) != SCHEMA_VERSION:
                raise RuntimeError("SCHEMA_UPGRADE_REQUIRED")
        return
    create_app()
    # Import all product models before taking the baseline snapshot.
    from .auth import models as auth_models  # noqa: F401
    from .characters import distillation_models  # noqa: F401
    from .materials import source_models  # noqa: F401
    from .materials.corpus import CorpusIndex  # noqa: F401
    from .materials.models import MaterialRow  # noqa: F401
    from .slice1 import models as config_models  # noqa: F401
    with db.engine.begin() as connection:
        Base.metadata.create_all(connection)
        connection.exec_driver_sql(_AUDIT_IMMUTABLE_SQL)
        connection.execute(text("""
            CREATE TABLE IF NOT EXISTS corpus_vectors (
              index_id varchar(64) NOT NULL REFERENCES corpus_indexes(index_id) ON DELETE CASCADE,
              position integer NOT NULL, chunk_id varchar(64) NOT NULL,
              owner_user_id varchar(64) NOT NULL, material_id varchar(64) NOT NULL,
              space varchar(64) NOT NULL, dimensions integer NOT NULL,
              page_no integer, section_id varchar(200), embedding vector NOT NULL,
              PRIMARY KEY(index_id, position))
        """))
        connection.execute(text("CREATE INDEX IF NOT EXISTS idx_corpus_vectors_scope "
            "ON corpus_vectors(owner_user_id,space,dimensions,material_id)"))
        connection.execute(text("CREATE TABLE desktop_schema(version integer NOT NULL)"))
        connection.execute(text("INSERT INTO desktop_schema VALUES (:version)"), {"version": SCHEMA_VERSION})
    # All further launches open this exact schema without running schema changes.


def local_session(db, owner_file):
    from .auth.models import LoginSessionRow, UserRow, utcnow
    from .auth.service import AuthService
    from .logging_audit.repositories import PgAuditLog
    audit = PgAuditLog(db)
    auth = AuthService(db, audit)
    owner = owner_file.read_text(encoding="utf8").strip() if owner_file.exists() else "user_" + uuid.uuid4().hex[:12]
    with db.session() as session:
        row = session.get(UserRow, owner)
        if row is None:
            if owner_file.exists():
                raise RuntimeError("OWNER_NOT_FOUND")
            # Persistent owner, never an administrator or a synthetic guest.
            session.add(UserRow(user_id=owner, email="local@desktop.invalid",
                password_hash=auth._hash_password(secrets.token_urlsafe(48)),
                display_name="我", role="user", status="approved", is_admin=False))
            session.flush()
            audit.append("auth.register.submitted", actor_id=owner, object_type="user",
                object_id=owner, result="desktop_created", session=session)
        if row is not None and row.status != "approved":
            raise RuntimeError("OWNER_DISABLED")
        token = secrets.token_urlsafe(32)
        session.add(LoginSessionRow(session_id="sess_" + uuid.uuid4().hex[:12], user_id=owner,
            session_token_hash=hashlib.sha256(token.encode()).hexdigest(),
            csrf_token_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
            expires_at=utcnow() + timedelta(days=14)))
    owner_file.parent.mkdir(parents=True, exist_ok=True)
    owner_file.write_text(owner, encoding="utf8")
    from .materials.source_settings import ReaderConfigIn, SourceSettings
    from .slice1.config_service import ConfigService
    sources = SourceSettings(ConfigService(db, audit=audit))
    if not sources.get(owner, "bilinote")["configuration"]:
        sources.save(owner, "bilinote", ReaderConfigIn(expected_revision=0, endpoint="builtin://bilinote"))
    return token


def memory_env(config, api_port, memory_port, database):
    proxy = f"http://127.0.0.1:{api_port}/api/internal/memory/v1"
    key = config["keys"]["proxy"]
    return {
        "HINDSIGHT_API_DATABASE_URL": database, "HINDSIGHT_API_PORT": str(memory_port),
        "HINDSIGHT_API_HOST": "127.0.0.1", "HINDSIGHT_API_TENANT_API_KEY": config["keys"]["memory"],
        "HINDSIGHT_API_TENANT_EXTENSION": "hindsight_api.extensions.builtin.tenant:ApiKeyTenantExtension",
        "HINDSIGHT_API_LLM_PROVIDER": "openai", "HINDSIGHT_API_LLM_BASE_URL": proxy,
        "HINDSIGHT_API_LLM_API_KEY": key, "HINDSIGHT_API_LLM_MODEL": "personal-memory",
        "HINDSIGHT_API_LLM_SEND_BANK_AS_USER": "true",
        "HINDSIGHT_API_EMBEDDINGS_PROVIDER": "openai",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_BASE_URL": proxy,
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_API_KEY": key,
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_MODEL": "personal-memory",
        "HINDSIGHT_API_EMBEDDINGS_OPENAI_DIMENSIONS": "2000",
        "HINDSIGHT_API_RERANKER_PROVIDER": "siliconflow",
        "HINDSIGHT_API_RERANKER_SILICONFLOW_BASE_URL": proxy,
        "HINDSIGHT_API_RERANKER_SILICONFLOW_API_KEY": key,
        "HINDSIGHT_API_RERANKER_SILICONFLOW_MODEL": "personal-memory",
        "HINDSIGHT_API_RERANKER_SEND_BANK_AS_HEADER": "true",
        "HINDSIGHT_API_ACCESS_LOG": "false", "HINDSIGHT_API_LOG_LEVEL": "warning",
        "HINDSIGHT_API_MCP_ENABLED": "false", "HINDSIGHT_API_LLM_TRACE_ENABLED": "false",
        "HINDSIGHT_API_LLM_OUTPUT_LANGUAGE": "Chinese",
        "HINDSIGHT_API_SKIP_LLM_VERIFICATION": "true",
    }


def preflight(config):
    roots, home = config["roots"], Path(config["home"])
    fingerprint = hashlib.sha256(json.dumps(roots, sort_keys=True).encode()).hexdigest()
    receipt = home / "config" / "component-health"
    marker = Path(roots["classroom"]) / ".preflight-ready"
    if (receipt.is_file() and marker.is_file() and
            receipt.read_text() == marker.read_text() == fingerprint):
        return
    emit("starting", component="video")
    run([Path(roots["video"]) / "python.exe", "-I", "-B", "-c",
        "import sys; from faster_whisper import WhisperModel; import imageio_ffmpeg; "
        "WhisperModel(sys.argv[1],device='cpu',compute_type='int8',cpu_threads=2,local_files_only=True); "
        "assert imageio_ffmpeg.get_ffmpeg_exe()",
        Path(roots["asr-small"]) / "small"], timeout=120)
    emit("starting", component="classroom")
    from .teaching.native_execution import NativeRunner
    runner = NativeRunner()
    examples = {
        "c": '#include <stdio.h>\nint main(){printf("ready");}',
        "cpp": '#include <iostream>\nint main(){std::cout<<"ready";}',
        "java": 'public class Main {public static void main(String[] a){System.out.println("ready");}}',
        "pytorch": 'import torch\nassert torch.tensor([1,2]).sum().item()==3\nprint("ready")',
    }
    for language, code in examples.items():
        result = runner.execute(str(uuid.uuid4()), "desktop-preflight", "desktop-preflight", language, code)
        if result["status"] != "success" or "ready" not in result["output"]:
            raise RuntimeError("COMPONENT_INCOMPATIBLE")
    receipt.write_text(fingerprint, encoding="ascii")
    marker.write_text(fingerprint, encoding="ascii")


def main():
    config = json.loads(sys.stdin.buffer.readline().decode("utf8"))
    home, roots, keys = Path(config["home"]), config["roots"], config["keys"]
    data = home / "data" / "postgres"
    owner_file = home / "config" / "owner"
    postgres = Path(roots["postgres"]) / "bin"
    api_port, db_port, memory_port = free_port(), free_port(), free_port()
    db_url, memory_url = configure(config, api_port, db_port, memory_port)
    children = []
    stop = threading.Event()
    from .desktop_lifecycle import wait_for_owner
    threading.Thread(target=lambda: (wait_for_owner(), stop.set()), daemon=True).start()
    db = None
    db_started = False
    try:
        emit("starting", component="postgres")
        # The cluster marker prevents accidentally operating an unrelated database.
        marker = home / "config" / "cluster"
        if data.is_symlink() or data.is_junction():
            raise RuntimeError("SCHEMA_NOT_OWNED")
        if marker.is_file() and marker.read_text() == "study-desktop-initializing-1":
            if (data / "postmaster.pid").exists():
                raise RuntimeError("SCHEMA_NOT_OWNED")
            if data.exists():
                recovery = home / "cache" / ("incomplete-cluster-" + uuid.uuid4().hex)
                os.replace(data, recovery)
        if not (data / "PG_VERSION").exists():
            if data.exists() and any(data.iterdir()):
                raise RuntimeError("SCHEMA_NOT_OWNED")
            data.parent.mkdir(parents=True, exist_ok=True)
            password_file = home / "config" / "initdb-password"
            password_file.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("study-desktop-initializing-1", encoding="ascii")
            password_file.write_text(keys["database"], encoding="utf8")
            try:
                run([postgres / "initdb.exe", "-D", data, "-U", "study_admin", "--pwfile", password_file,
                    "--auth=scram-sha-256", "--encoding=UTF8", "--locale=C"])
            finally:
                password_file.unlink(missing_ok=True)
            marker.write_text("study-desktop-cluster-1", encoding="ascii")
        elif not marker.is_file() or marker.read_text() != "study-desktop-cluster-1":
            raise RuntimeError("SCHEMA_NOT_OWNED")
        run([postgres / "pg_ctl.exe", "-D", data, "-o",
            f'-p {db_port} -h 127.0.0.1 -c logging_collector=off -c log_min_messages=panic '
            '-c shared_buffers=64MB -c max_connections=50',
            "-l", home / "cache" / "postgres-start.log", "-w", "start"])
        db_started = True
        import psycopg
        from psycopg import sql
        with psycopg.connect(host="127.0.0.1", port=db_port, user="study_admin",
                            password=keys["database"], dbname="postgres", autocommit=True) as connection:
            for name, password in (("study", keys["database"]), ("study_memory", keys["memory"])):
                if not connection.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)).fetchone():
                    connection.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(name), sql.Literal(password)))
                if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
                    connection.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(name)))
                with psycopg.connect(host="127.0.0.1", port=db_port, user="study_admin",
                        password=keys["database"], dbname=name, autocommit=True) as target:
                    target.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    target.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        from .logging_audit.db import Database
        db = Database()
        initialize(db)
        token = local_session(db, owner_file)
        preflight(config)
        emit("starting", component="core")
        child = subprocess.Popen([sys.executable, "-m", "app.desktop_child", "api", str(api_port)],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=FLAGS)
        children.append(child)
        emit("starting", component="memory")
        environment = os.environ.copy() | memory_env(config, api_port, memory_port, memory_url)
        environment["STUDY_MEMORY_DESKTOP"] = "1"
        memory = subprocess.Popen([str(Path(roots["memory"]) / "python.exe"), "-m", "app.desktop_child",
            "memory", config["memoryScript"]],
            env=environment, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=FLAGS)
        children.append(memory)
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for port, route in ((api_port, "/healthz"), (memory_port, "/health")):
            component = "core" if port == api_port else "memory"
            emit("starting", component=component, probe="health", port=port)
            request = urllib.request.Request(f"http://127.0.0.1:{port}{route}",
                headers={"Authorization": "Bearer " + keys["memory"]} if port == memory_port else {})
            deadline = time.monotonic() + 300
            last_status = None
            while time.monotonic() < deadline:
                if stop.is_set() or any(process.poll() is not None for process in children):
                    raise RuntimeError("SERVICE_FAILED")
                try:
                    with opener.open(request, timeout=2) as response:
                        if response.status == 200:
                            break
                except Exception as error:
                    status = getattr(error, "code", 0)
                    if status != last_status:
                        emit("starting", component=component, probe_status=status, port=port)
                        last_status = status
                    time.sleep(.5)
            else:
                raise RuntimeError("SERVICE_FAILED")
        emit("ready", port=api_port, token=token)
        while not stop.wait(1):
            if any(process.poll() is not None for process in children):
                raise RuntimeError("SERVICE_FAILED")
    except Exception as exc:
        code = str(exc) if str(exc) in ("SCHEMA_NOT_OWNED", "SCHEMA_UPGRADE_REQUIRED",
            "OWNER_NOT_FOUND", "OWNER_DISABLED", "COMPONENT_INCOMPATIBLE") else "SERVICE_FAILED"
        import traceback
        location = traceback.extract_tb(exc.__traceback__)[-1]
        from pydantic import ValidationError
        emit("failed", code=code, error_type=type(exc).__name__, line=location.lineno,
            module=exc.name if isinstance(exc, ModuleNotFoundError) else None,
            fields=[list(item["loc"]) for item in exc.errors(include_input=False, include_context=False)]
            if isinstance(exc, ValidationError) else [])
    finally:
        for process in reversed(children):
            if process.poll() is None:
                process.stdin.close()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        if db:
            db.dispose()
        if db_started:
            try:
                backup = home / "backups" / "database"
                backup.mkdir(parents=True, exist_ok=True)
                day = time.strftime("%Y%m%d", time.gmtime())
                for name in ("study", "study_memory"):
                    destination = backup / f"{name}-{day}.dump"
                    if not destination.exists():
                        temporary = destination.with_suffix(".part")
                        run([postgres / "pg_dump.exe", "-h", "127.0.0.1", "-p", str(db_port),
                            "-U", "study_admin", "-d", name, "-Fc", "-f", temporary],
                            env=os.environ.copy() | {"PGPASSWORD": keys["database"]}, timeout=60)
                        os.replace(temporary, destination)
                for name in ("study", "study_memory"):
                    old = sorted(backup.glob(name + "-*.dump"), reverse=True)
                    for file in old[7:]:
                        file.unlink()
            finally:
                run([postgres / "pg_ctl.exe", "-D", data, "-m", "fast", "-w", "stop"])


if __name__ == "__main__":
    main()

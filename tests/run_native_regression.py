"""Run root pytest against an owned temporary PostgreSQL schema, never public."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "backend"))
from app.logging_audit.db import Database, get_database_url
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def main():
    schema = "native_validation_" + uuid.uuid4().hex
    base_url = make_url(get_database_url())
    if base_url.get_backend_name() != "postgresql":
        raise RuntimeError("isolated regression requires PostgreSQL")
    admin = create_engine(base_url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated = base_url.update_query_dict({"options": "-csearch_path=" + schema})
    url = isolated.render_as_string(hide_password=False)
    os.environ.update(LEARNING_DATABASE_URL=url, DATABASE_URL=url,
        SECRET_KEY="synthetic-native-regression-secret-" + uuid.uuid4().hex,
        PROJECT_NAME="Synthetic native regression", FIRST_SUPERUSER="synthetic-admin@example.com",
        FIRST_SUPERUSER_PASSWORD="synthetic-native-password-12345", CONTENT_TRACE_ENABLED="0",
        OTEL_ENABLED="0", LOGFIRE_TOKEN="", LOGFIRE_OTLP_ENDPOINT="", PYDANTIC_AI_NO_BANNER="1")
    output = ROOT / ".backup" / "native-pydanticai" / schema
    output.mkdir(parents=True, exist_ok=True)
    db = None
    try:
        from app.slice3_app import Slice3State
        from app.agents.task_binding import AgentBudgetRow
        from app.models import SQLModel
        from app.infrastructure.db import init_db
        from sqlmodel import Session
        db = Database(url)
        Slice3State(db)
        db.create_all()
        SQLModel.metadata.create_all(db.engine)
        with Session(db.engine) as session:
            init_db(session)
        log = output / "pytest.log"
        args = sys.argv[1:] or ["tests", "-q", "--tb=short"]
        print("Isolated schema initialized: " + schema, flush=True)
        print("Evidence: " + str(log), flush=True)
        with log.open("w", encoding="utf8") as stream:
            result = subprocess.run([sys.executable, "-m", "pytest", *args], cwd=ROOT,
                stdout=stream, stderr=subprocess.STDOUT, env=os.environ.copy())
        lines = log.read_text(encoding="utf8", errors="replace").splitlines()
        for line in lines[-60:]:
            print(line)
        return result.returncode
    finally:
        if db:
            db.dispose()
        # Only the exact random schema created by this invocation is ever removed.
        if not schema.startswith("native_validation_") or len(schema) != 50:
            raise RuntimeError("refusing unexpected schema cleanup")
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


if __name__ == "__main__":
    sys.exit(main())

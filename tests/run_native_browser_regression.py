"""Owned native browser run with a random PostgreSQL schema and production assembler.

Run after building the frontend. Set LEARNING_DATABASE_URL to the local test
PostgreSQL database, PW_ROOT to the node_modules directory containing Playwright,
and TEST_NODE or install node on PATH. TEST_BROWSER is optional when Playwright's
managed browser is installed. The suite injects genuine SDK FunctionModels for
synthetic intake/planning/chat/teaching responses, not a production model fallback.
It validates desktop/mobile rendering, stream persistence, and text-only teaching;
it does not validate real suppliers, TTS, assessments, or a complete lesson.
Only processes started here and the exact randomly created schema are cleaned up.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "backend"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "support"))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve():
    from app.assembler import create_app
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel
    from native_model_support import _request
    from test_teaching_assessments import LearningAdapter
    from test_v2_intake import InterviewAdapter
    from common_fixtures import SyntheticSearchAdapter
    import uvicorn

    evidence = Path(os.environ["TEST_OUTPUT"]) / "native-model-calls.jsonl"

    def record(role, info):
        with evidence.open("a", encoding="utf8") as stream:
            stream.write(json.dumps({"role": role, "output_mode": info.model_request_parameters.output_mode,
                "sdk_model": "FunctionModel"}) + "\n")

    def planning(messages, info):
        translated, options = _request(messages, info)
        schema = info.model_request_parameters.output_object
        props = schema.json_schema.get("properties", {}) if schema else {}
        adapter = InterviewAdapter(scope="course", followups=2)
        record("intake" if "ready_to_plan" in props else "planning", info)
        return ModelResponse(parts=[TextPart(adapter.generate(translated, **options)["content"])])

    def teaching(messages, info):
        translated, options = _request(messages, info)
        record("teaching", info)
        raw = LearningAdapter().generate(translated, **options)
        return ModelResponse(parts=[TextPart(raw["content"])])

    answer = "Native SDK synthetic browser answer: keys identify values and preserve the question context."
    def chat(messages, info):
        record("chat", info)
        return ModelResponse(parts=[TextPart(answer)])

    async def chat_stream(messages, info):
        import asyncio
        record("chat_stream", info)
        for offset in range(0, len(answer), 12):
            await asyncio.sleep(0.02)
            yield answer[offset:offset + 12]

    app = create_app()
    learning = next(route.app for route in app.routes
        if getattr(route, "path", None) in ("", "/") and hasattr(route, "app"))
    state = learning.state.slice3
    state.planning.model_adapter_factory = lambda *args: FunctionModel(planning, model_name="synthetic-native-browser-planning")
    from app.agents.task_binding import bind_task_deps
    from app.slice1.errors import error
    def teaching_factory(owner, task_id):
        view = state.teaching.configs.latest_active_for_owner(owner, "content_model")
        if not view:
            raise error("config_not_found")
        model = FunctionModel(teaching, model_name="synthetic-native-browser-teaching")
        model.config_id, model.config_version, model.endpoint = view["config_id"], view["config_version"], view["endpoint"]
        model.agent_deps = bind_task_deps(state.db, model, actor_id=owner, task_id=task_id,
            disclosures=state.teaching.disclosures)
        def authorized_teaching(messages, info):
            deps = model.agent_deps
            deps.authorize_payload(b"synthetic SDK request", model.endpoint, deps.disclosure_scope)
            deps.budget.consume("wire")
            return teaching(messages, info)
        model.function = authorized_teaching
        return model
    teaching_factory.config_for_owner = lambda owner: state.teaching.configs.latest_active_for_owner(owner, "content_model")
    state.teaching.model_adapter_factory = teaching_factory
    class SyntheticImage:
        def generate_image(self, prompt):
            self.authorize_request()
            from app.planning.ink import InkRecipe, render_ink
            with evidence.open("a", encoding="utf8") as stream:
                stream.write(json.dumps({"role": "cover", "output_mode": "image", "sdk_model": "synthetic-image"}) + "\n")
            return render_ink(InkRecipe(), 1)
        def close(self):
            pass
    state.features.image_adapter_factory = lambda owner, task_id: SyntheticImage()
    state.chat.model_adapter_factory = lambda *args: FunctionModel(chat, stream_function=chat_stream,
        model_name="synthetic-native-browser-chat")
    state.planning.search_adapter_factory = lambda *args: SyntheticSearchAdapter()
    state.chat.search_adapter_factory = lambda *args: None
    state.teaching.tts_adapter_factory = lambda *args: None
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["TEST_PORT"]), access_log=False)


def main():
    from app.logging_audit.db import Database, get_database_url
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    schema = "course_library_validation_" + uuid.uuid4().hex
    output = ROOT / ".backup" / "native-browser" / schema
    output.mkdir(parents=True)
    url = make_url(get_database_url())
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("browser regression requires PostgreSQL")
    admin = create_engine(url)
    processes = []
    streams = []
    db = None
    created = False
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        isolated = url.update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
        backend_port, frontend_port = free_port(), free_port()
        while frontend_port == backend_port or backend_port == 8000:
            backend_port, frontend_port = free_port(), free_port()
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(("LEARNING_REAL_", "LOGFIRE_", "LANGFUSE_")):
                env.pop(key)
        env.update(LEARNING_DATABASE_URL=isolated, DATABASE_URL=isolated,
            SECRET_KEY="synthetic-browser-secret-" + uuid.uuid4().hex,
            FIRST_SUPERUSER="synthetic-browser-admin@example.com", FIRST_SUPERUSER_PASSWORD="synthetic-password-12345",
            PROJECT_NAME="Synthetic native browser", CONTENT_TRACE_ENABLED="0", OTEL_ENABLED="0",
            LEARNING_DEV_SYNTHETIC_MODEL="0", PYDANTIC_AI_NO_BANNER="1", SENTRY_DSN="",
            APP_ENCRYPTION_KEY="synthetic-browser-encryption-key", TEST_PORT=str(backend_port),
            FRONTEND_HOST=f"http://127.0.0.1:{frontend_port}", TEST_FRONTEND=f"http://127.0.0.1:{frontend_port}",
            TEST_BACKEND=f"http://127.0.0.1:{backend_port}", TEST_OUTPUT=str(output),
            TEST_PYTHON=sys.executable, PYTHONPATH=str(ROOT / "server" / "backend"))
        import shutil
        node = env.get("TEST_NODE") or shutil.which("node")
        if not node or not env.get("PW_ROOT"):
            raise RuntimeError("set TEST_NODE (or install node on PATH) and PW_ROOT to Playwright node_modules")
        if not (ROOT / "frontend/dist/index.html").is_file():
            raise RuntimeError("build frontend before browser regression")
        os.environ.update(env)
        from app.slice3_app import Slice3State
        from app.agents.task_binding import AgentBudgetRow
        from app.models import SQLModel
        from app.infrastructure.db import init_db
        from sqlmodel import Session
        db = Database(isolated)
        Slice3State(db)
        db.create_all()
        SQLModel.metadata.create_all(db.engine)
        with Session(db.engine) as session:
            init_db(session)
        with db.engine.connect() as conn:
            assert conn.execute(text("select current_schema()")).scalar() == schema
        print(json.dumps({"schema": schema, "backend": env["TEST_BACKEND"], "frontend": env["TEST_FRONTEND"],
            "evidence": str(output)}), flush=True)
        commands = [[sys.executable, str(Path(__file__).resolve()), "--serve"],
            [node, str(ROOT / "frontend/node_modules/vite/bin/vite.js"), "preview", "--host", "127.0.0.1",
             "--port", str(frontend_port), "--strictPort"]]
        for name, command in zip(("backend", "frontend"), commands):
            stream = (output / (name + ".log")).open("w", encoding="utf8")
            streams.append(stream)
            processes.append(subprocess.Popen(command, cwd=ROOT / "frontend" if name == "frontend" else ROOT,
                env=env, stdout=stream, stderr=subprocess.STDOUT))
        for endpoint in (env["TEST_BACKEND"] + "/api/auth/guest", env["TEST_FRONTEND"]):
            deadline = time.monotonic() + 45
            while True:
                if any(proc.poll() is not None for proc in processes):
                    raise RuntimeError("owned server exited before readiness; inspect evidence logs")
                try:
                    with urllib.request.urlopen(endpoint, timeout=1) as response:
                        if response.status == 200:
                            break
                except Exception:
                    if time.monotonic() > deadline:
                        raise RuntimeError("owned server readiness deadline exceeded")
                    time.sleep(0.25)
        script = Path(os.environ.get("NATIVE_BROWSER_SCRIPT", str(ROOT / "tests/native_ai_browser.mjs")))
        with (output / "browser.log").open("w", encoding="utf8") as stream:
            result = subprocess.run([node, str(script)], cwd=ROOT, env=env, stdout=stream,
                stderr=subprocess.STDOUT, timeout=360)
        print((output / "browser.log").read_text(encoding="utf8", errors="replace"), flush=True)
        calls = [json.loads(line) for line in (output / "native-model-calls.jsonl").read_text().splitlines()]
        roles = {item["role"] for item in calls}
        print(json.dumps({"native_roles": sorted(roles), "calls": len(calls), "browser_exit": result.returncode}), flush=True)
        if result.returncode == 0:
            expected = {"teaching"} if script.name == "teaching_assessment_browser.mjs" else {
                "intake", "planning", "teaching", "chat", "chat_stream"}
            assert expected <= roles
        return result.returncode
    finally:
        for proc in reversed(processes):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        for stream in streams:
            stream.close()
        if db:
            db.dispose()
        if created:
            if not schema.startswith("course_library_validation_") or len(schema) != 58:
                raise RuntimeError("refusing unexpected schema cleanup")
            with admin.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
        print("Owned processes stopped; owned schema removed.", flush=True)


def expire_authorization(conversation_id):
    from datetime import timedelta
    from sqlalchemy import select, text
    from app.logging_audit.db import Database
    from app.teaching.authorization import LessonAuthorizationRow
    from app.slice1.models import DisclosureGrantRow
    from app.planning.models import utcnow
    db = Database()
    try:
        with db.session() as sess:
            schema = sess.scalar(text("select current_schema()"))
            if not schema.startswith("course_library_validation_") or len(schema) != 58:
                raise RuntimeError("refusing authorization fixture outside owned browser schema")
            rows = sess.scalars(select(LessonAuthorizationRow).where(
                LessonAuthorizationRow.conversation_id == conversation_id)).all()
            if not rows:
                raise RuntimeError("owned conversation authorization missing")
            for row in rows:
                root = sess.get(DisclosureGrantRow, row.content_root_grant_id)
                root.expires_at = utcnow() - timedelta(seconds=1)
        print(json.dumps({"expired": True}))
    finally:
        db.dispose()


if __name__ == "__main__":
    if "--expire-authorization" in sys.argv:
        expire_authorization(sys.argv[2])
    else:
        sys.exit(serve() if "--serve" in sys.argv else main())

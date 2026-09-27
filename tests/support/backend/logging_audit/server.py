"""T02/T03：stdlib HTTP 服务（SSE + REST + trace 中间件）。

开发环境等价实现 FastAPI 契约：
- W3C traceparent 验证/生成
- POST /tasks 创建任务（事务内写状态+首事件）
- GET  /tasks/{id}/status   REST 全量状态
- GET  /tasks/{id}/events   SSE；支持 Last-Event-ID 补取；过期 410
- GET  /tasks/{id}/events.json?after_sequence=N  REST 事件缺口
- POST /audit               追加审计
- GET  /audit               角色查询 + 查询留痕

鉴权：X-Actor-Id / X-Role 头（生产替换为会话 JWT；所有权语义不变）。
仅监听 127.0.0.1，不对外暴露。
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .audit import AuditAccessDeniedError, AuditImmutabilityError, AuditWriteError
from .contracts import ContractError
from .logger import MemoryLogStream, StructuredLogger
from .storage import SqliteAuditLog, SqliteTaskEventStore
from .task_events import AccessDeniedError, EventsExpiredError
from .trace import ensure_trace, trace_context

_TASK_RE = re.compile(r"^/tasks/([A-Za-z0-9_\-]+)(?:/(status|events(?:\.json)?))?$")


class AppState:
    def __init__(
        self,
        db_path: str = ":memory:",
        logger: Optional[StructuredLogger] = None,
    ) -> None:
        self.tasks = SqliteTaskEventStore(db_path=db_path)
        self.audit = SqliteAuditLog(db_path=db_path)
        self.logger = logger or StructuredLogger("api", "test", stream=MemoryLogStream())
        self.log_stream = self.logger.stream
        # 观测导出器可注入（故障注入用）
        self.exporter = None

    def close(self) -> None:
        self.tasks.close()
        self.audit.close()


class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: AppState = None  # type: ignore[assignment]
    quiet = True

    def log_message(self, fmt: str, *args: Any) -> None:
        if not self.quiet:
            super().log_message(fmt, *args)

    # ---- helpers ----
    def _header(self, name: str, default: str = "") -> str:
        return self.headers.get(name, default) or default

    def _actor(self) -> str:
        return self._header("X-Actor-Id") or "anonymous"

    def _role(self) -> str:
        return self._header("X-Role") or "app"

    def _send_json(self, status: int, payload: dict, extra_headers: Optional[Dict[str, str]] = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self._header("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise ContractError("INVALID_JSON", str(exc)) from exc

    def _with_trace(self, fn: Callable[[], None]) -> None:
        ctx = ensure_trace(self._header("traceparent") or None)
        with trace_context(ctx):
            self._current_trace_id = ctx.trace_id
            fn()

    def _error(self, status: int, code: str, message: str, **extra: Any) -> None:
        self._send_json(status, {"error_code": code, "message": message, **extra})

    # ---- routing ----
    def do_GET(self) -> None:  # noqa: N802
        self._with_trace(self._route_get)

    def do_POST(self) -> None:  # noqa: N802
        self._with_trace(self._route_post)

    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        m = _TASK_RE.match(path)
        try:
            if m and m.group(2) == "status":
                return self._get_status(m.group(1))
            if m and m.group(2) == "events.json":
                after = int(qs.get("after_sequence", ["0"])[0] or "0")
                return self._get_events_json(m.group(1), after)
            if m and m.group(2) == "events":
                return self._get_events_sse(m.group(1))
            if path == "/audit":
                return self._get_audit()
            if path == "/healthz":
                return self._send_json(200, {"ok": True})
            return self._error(404, "NOT_FOUND", "unknown path")
        except AccessDeniedError as exc:
            self._audit_access_denied(str(exc))
            return self._error(403, "ACCESS_DENIED", "not owner")
        except AuditAccessDeniedError:
            return self._error(403, "ACCESS_DENIED", "role not allowed")
        except EventsExpiredError as exc:
            return self._error(410, "EVENTS_EXPIRED", exc.reason, last_sequence=exc.last_sequence)
        except ContractError as exc:
            return self._error(400, exc.code, exc.message)

    def _route_post(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_body()
            if path == "/tasks":
                return self._post_task(body)
            if path == "/tasks/events":
                return self._post_event(body)
            if path == "/audit":
                return self._post_audit(body)
            return self._error(404, "NOT_FOUND", "unknown path")
        except AccessDeniedError as exc:
            self._audit_access_denied(str(exc))
            return self._error(403, "ACCESS_DENIED", "not owner")
        except AuditAccessDeniedError:
            return self._error(403, "ACCESS_DENIED", "role not allowed")
        except AuditWriteError:
            return self._error(503, "AUDIT_WRITE_FAILED", "audit write failed")
        except AuditImmutabilityError:
            return self._error(403, "ACCESS_DENIED", "audit immutable")
        except ContractError as exc:
            return self._error(400, exc.code, exc.message)

    # ---- handlers ----
    def _post_task(self, body: dict) -> None:
        task_id = body.get("task_id") or ""
        owner = body.get("owner_user_id") or self._actor()
        subject_id = body.get("subject_id") or ""
        stage = body.get("stage") or "upload"
        if not task_id or not subject_id:
            raise ContractError("INVALID_FIELD", "task_id/subject_id required")
        event = self.state.tasks.create_task(
            task_id,
            owner,
            subject_id,
            stage,
            generation=int(body.get("generation") or 1),
            attempt=int(body.get("attempt") or 1),
            progress_total=int(body.get("progress_total") or 0),
            template_id=body.get("template_id"),
            template_params=body.get("template_params") or {},
        )
        self.state.logger.log(
            "task.accepted",
            level="INFO",
            task_id=task_id,
            attempt=int(body.get("attempt") or 1),
            request_id=self._header("X-Request-Id") or None,
            actor_id=owner,
            stage=stage,
            status="started",
            trace_id=getattr(self, "_current_trace_id", None),
        )
        self._send_json(201, {"task_id": task_id, "sequence": event.sequence, "event_id": event.event_id})

    def _post_event(self, body: dict) -> None:
        task_id = body.get("task_id") or ""
        actor = body.get("actor_id") or self._actor()
        event = self.state.tasks.record(
            task_id,
            stage=body.get("stage") or "save",
            status=body.get("status") or "succeeded",
            error_code=body.get("error_code"),
            progress_current=body.get("progress_current"),
            progress_total=body.get("progress_total"),
            duration_ms=body.get("duration_ms"),
            generation=body.get("generation"),
            attempt=body.get("attempt"),
            actor_id=actor,
            diagnostics_only=bool(body.get("diagnostics_only")),
        )
        event_name = {
            "started": "task.started",
            "succeeded": "task.stage.succeeded",
            "partial": "task.stage.partial",
            "failed": "task.stage.failed",
            "retry_scheduled": "task.retry_scheduled",
            "cancelled": "task.cancelled",
            "stale_discarded": "task.stale_discarded",
        }.get(body.get("status") or "succeeded", "task.stage.failed")
        self.state.logger.log(
            event_name,
            level="INFO",
            task_id=task_id,
            stage=body.get("stage") or "save",
            status=body.get("status") or "succeeded",
            error_code=body.get("error_code"),
            duration_ms=body.get("duration_ms"),
            attempt=body.get("attempt"),
        )
        self._send_json(201, {"event_id": event.event_id, "sequence": event.sequence, "status": event.status})

    def _get_status(self, task_id: str) -> None:
        status = self.state.tasks.full_status(task_id, actor_id=self._actor())
        self._send_json(200, status)

    def _get_events_json(self, task_id: str, after_sequence: int) -> None:
        events = self.state.tasks.get_events(task_id, actor_id=self._actor(), after_sequence=after_sequence)
        self._send_json(
            200,
            {
                "task_id": task_id,
                "events": [
                    {
                        "event_id": e.event_id,
                        "sequence": e.sequence,
                        "stage": e.stage,
                        "status": e.status,
                        "error_code": e.error_code,
                        "created_at": e.created_at,
                        "progress_current": e.progress_current,
                        "progress_total": e.progress_total,
                    }
                    for e in events
                ],
            },
        )

    def _get_events_sse(self, task_id: str) -> None:
        last_event_id = self._header("Last-Event-ID") or None
        last_seq_hdr = self._header("X-Last-Sequence") or None
        last_sequence = int(last_seq_hdr) if last_seq_hdr and last_seq_hdr.isdigit() else None

        try:
            events, next_seq = self.state.tasks.replay_sse(
                task_id,
                actor_id=self._actor(),
                last_event_id=last_event_id,
                last_sequence=last_sequence,
            )
        except EventsExpiredError as exc:
            # SSE 过期：返回 410 JSON（客户端改 REST 全量）
            return self._error(410, "EVENTS_EXPIRED", exc.reason, last_sequence=exc.last_sequence)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        for e in events:
            payload = {
                "event_id": e.event_id,
                "sequence": e.sequence,
                "stage": e.stage,
                "status": e.status,
                "error_code": e.error_code,
                "progress_current": e.progress_current,
                "progress_total": e.progress_total,
                "created_at": e.created_at,
                "generation": e.generation,
                "attempt": e.attempt,
            }
            chunk = f"id: {e.event_id}\nevent: task\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            self.wfile.write(chunk.encode("utf-8"))
        # 尾注：告知客户端当前最大序号，然后关闭（批量回放语义）
        tail = f": cursor {next_seq}\n\n"
        self.wfile.write(tail.encode("utf-8"))
        self.wfile.flush()
        self.close_connection = True

    def _post_audit(self, body: dict) -> None:
        rec = self.state.audit.append(
            body.get("event") or "access.denied",
            actor_id=body.get("actor_id") or self._actor(),
            result=body.get("result") or "success",
            object_type=body.get("object_type"),
            object_id=body.get("object_id"),
            trace_id=body.get("trace_id") or getattr(self, "_current_trace_id", None),
            reason=body.get("reason"),
            admin_id=body.get("admin_id"),
            role=body.get("role") or self._role(),
            target_actor_id=body.get("target_actor_id"),
        )
        self._send_json(201, {"audit_id": rec.audit_id, "content_hash": rec.content_hash})

    def _get_audit(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        records = self.state.audit.query(
            actor_id=self._actor(),
            role=self._role(),
            event=qs.get("event", [None])[0],
            object_type=qs.get("object_type", [None])[0],
            object_id=qs.get("object_id", [None])[0],
            limit=int(qs.get("limit", ["100"])[0]),
        )
        self._send_json(200, {"records": [r.to_record() for r in records]})

    def _audit_access_denied(self, reason: str) -> None:
        try:
            self.state.audit.append(
                "access.denied",
                actor_id=self._actor(),
                result="denied",
                resource_type="task",
                resource_id=urlparse(self.path).path,
                reason=reason,
                trace_id=getattr(self, "_current_trace_id", None),
            )
        except Exception:  # noqa: BLE001
            pass


def make_server(host: str = "127.0.0.1", port: int = 0, state: Optional[AppState] = None) -> ThreadingHTTPServer:
    app_state = state or AppState()
    handler = type("BoundHandler", (ApiHandler,), {"state": app_state, "quiet": True})

    class QuietServer(ThreadingHTTPServer):
        def handle_error(self, request, client_address):  # noqa: ANN001
            # SSE/客户端提前断开属于正常场景，不打印堆栈
            import sys as _sys

            exc = _sys.exc_info()[1]
            if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, TimeoutError, BrokenPipeError)):
                return
            super().handle_error(request, client_address)

    httpd = QuietServer((host, port), handler)
    return httpd


def start_in_thread(host: str = "127.0.0.1", port: int = 0, state: Optional[AppState] = None) -> Tuple[ThreadingHTTPServer, threading.Thread, AppState]:
    app_state = state or AppState()
    httpd = make_server(host, port, app_state)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, app_state

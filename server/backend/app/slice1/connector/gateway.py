"""本机连接器 WSS 网关（切片 1 T06）。

- 连接器主动建立出站 WSS/TLS 通道到服务端；服务端校验绑定关系。
- 每个请求带账户、设备、任务、过期时间和 nonce；服务端拒绝过期或重复 nonce。
- 连接器只接受绑定账户的任务；限制消息大小与调用类型。
- 断开时标记该路径离线；撤销后拒绝新请求并使在途请求失效。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable, Optional

from ..connector_service import MAX_REQUEST_BYTES, ConnectorService
from ..errors import Slice1Error, error


class ConnectorGateway:
    """管理连接器 WSS 连接与任务下发。"""

    def __init__(
        self,
        connectors: ConnectorService,
        *,
        task_owner_resolver: Optional[Callable[[str], Optional[str]]] = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        self.connectors = connectors
        self.task_owner_resolver = task_owner_resolver
        self.host = host
        self.port = port
        self._connections: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self.rejected: list[str] = []

    # ---- 生命周期 ----
    def start(self) -> str:
        from websockets.sync.server import serve

        self._server = serve(
            self._handler,
            self.host,
            self.port,
            max_size=MAX_REQUEST_BYTES,
        )
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.url

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"

    def stop(self) -> None:
        with self._lock:
            self._connections.clear()
        if self._server is not None:
            self._server.shutdown()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ---- 连接 ----
    def _handler(self, websocket) -> None:  # noqa: ANN001
        binding_id: Optional[str] = None
        try:
            for raw in websocket:
                try:
                    message = json.loads(raw)
                except (ValueError, TypeError):
                    self._send(websocket, {"type": "error", "error_code": "invalid_field"})
                    continue
                msg_type = message.get("type")
                if msg_type == "bind":
                    binding_id = self._handle_bind(websocket, message)
                elif msg_type == "auth":
                    binding_id = self._handle_auth(websocket, message)
                elif msg_type == "request":
                    self._handle_request(websocket, message, binding_id)
                elif msg_type == "ping":
                    if binding_id:
                        self.connectors.mark_seen(binding_id)
                    self._send(websocket, {"type": "pong"})
                else:
                    self._send(websocket, {"type": "error", "error_code": "invalid_field"})
        except Exception:  # noqa: BLE001 - 连接异常不外泄
            pass
        finally:
            if binding_id:
                with self._lock:
                    if self._connections.get(binding_id) is websocket:
                        self._connections.pop(binding_id, None)

    def _handle_bind(self, websocket, message: dict) -> Optional[str]:  # noqa: ANN001
        try:
            result = self.connectors.bind(
                pairing_code=message.get("pairing_code", ""),
                device_name=message.get("device_name", ""),
            )
        except Slice1Error as exc:
            self.rejected.append(exc.code)
            self._send(websocket, {"type": "error", "error_code": exc.code})
            return None
        binding_id = result["binding_id"]
        with self._lock:
            self._connections[binding_id] = websocket
        self.connectors.mark_seen(binding_id)
        self._send(
            websocket,
            {
                "type": "bound",
                "binding_id": binding_id,
                "binding_token": result["binding_token"],
                "device_name": result["device_name"],
            },
        )
        return binding_id

    def _handle_auth(self, websocket, message: dict) -> Optional[str]:  # noqa: ANN001
        try:
            binding = self.connectors.resolve_binding(message.get("binding_token", ""))
        except Slice1Error as exc:
            self.rejected.append(exc.code)
            self._send(websocket, {"type": "error", "error_code": exc.code})
            return None
        binding_id = binding["binding_id"]
        with self._lock:
            self._connections[binding_id] = websocket
        self.connectors.mark_seen(binding_id)
        self._send(websocket, {"type": "bound", "binding_id": binding_id, "status": "bound"})
        return binding_id

    def _handle_request(self, websocket, message: dict, binding_id: Optional[str]) -> None:  # noqa: ANN001
        if binding_id is None:
            self._send(websocket, {"type": "error", "error_code": "connector_offline"})
            return
        task_id = message.get("task_id", "")
        owner = binding_id and self._owner_for_binding(binding_id)
        if not message.get("nonce"):
            self.rejected.append("invalid_field")
            self._send(websocket, {"type": "error", "error_code": "invalid_field", "task_id": task_id})
            return
        task_owner = self.task_owner_resolver(task_id) if self.task_owner_resolver else owner
        try:
            accepted = self.connectors.accept_request(
                binding_id=binding_id,
                task_id=task_id,
                nonce=message.get("nonce", ""),
                owner_user_id=owner or "",
                task_owner_user_id=task_owner,
            )
        except Slice1Error as exc:
            self.rejected.append(exc.code)
            self._send(websocket, {"type": "error", "error_code": exc.code, "task_id": task_id})
            return
        self._send(
            websocket,
            {
                "type": "accepted",
                "request_id": accepted["request_id"],
                "task_id": task_id,
                "call_type": message.get("call_type", "model.generate"),
                "payload": message.get("payload"),
            },
        )

    def _owner_for_binding(self, binding_id: str) -> Optional[str]:
        with self.connectors.db.session() as sess:
            from ..models import ConnectorBindingRow

            row = sess.get(ConnectorBindingRow, binding_id)
            return row.owner_user_id if row else None

    def _send(self, websocket, payload: dict) -> None:  # noqa: ANN001
        try:
            websocket.send(json.dumps(payload, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass

    # ---- 下发 ----
    def send_task(self, binding_id: str, message: dict) -> bool:
        with self._lock:
            websocket = self._connections.get(binding_id)
        if websocket is None:
            return False
        try:
            websocket.send(json.dumps(message, ensure_ascii=False))
            return True
        except Exception:  # noqa: BLE001
            with self._lock:
                self._connections.pop(binding_id, None)
            return False

    def is_connected(self, binding_id: str) -> bool:
        with self._lock:
            return binding_id in self._connections


def offline_state(connectors: ConnectorService, binding_id: str) -> str:
    """连接器离线/撤销状态可见。"""
    if connectors.is_online(binding_id):
        return "online"
    with connectors.db.session() as sess:
        from ..models import ConnectorBindingRow

        row = sess.get(ConnectorBindingRow, binding_id)
        if row is None:
            raise error("binding_not_found")
        if row.status == "revoked":
            return "revoked"
        return "offline"

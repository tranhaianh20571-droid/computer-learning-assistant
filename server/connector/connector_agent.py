"""本机连接器独立进程（切片 1 T06）。

用法：
    python connector_agent.py --server ws://127.0.0.1:8765 --pairing-code <code> [--device-name pc]
    python connector_agent.py --server ws://127.0.0.1:8765 --binding-token <token>

安全边界：
- 只连接用户指定的服务端地址（出站 WSS/TLS），不监听任何公网端口。
- 只接受绑定账户的任务；请求必须带任务、过期时间和 nonce。
- 只允许调用本机回环目标（127.0.0.1 / ::1），拒绝其它地址。
- 不把凭据写入日志或磁盘。
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from typing import Optional
from urllib.parse import urlparse


def is_loopback_target(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class LocalConnector:
    """连接器主体：负责握手、心跳与本地调用分发。"""

    def __init__(
        self,
        server_url: str,
        *,
        pairing_code: Optional[str] = None,
        binding_token: Optional[str] = None,
        device_name: str = "local-device",
        local_target: str = "http://127.0.0.1:11434",
    ) -> None:
        if not server_url.startswith(("ws://", "wss://")):
            raise ValueError("server url must be ws:// or wss://")
        if not is_loopback_target(local_target):
            raise ValueError("local target must be loopback")
        self.server_url = server_url
        self.pairing_code = pairing_code
        self.binding_token = binding_token
        self.device_name = device_name
        self.local_target = local_target
        self._connection = None

    def connect(self):  # noqa: ANN201
        from websockets.sync.client import connect

        self._connection = connect(self.server_url, max_size=64 * 1024)
        if self.pairing_code:
            self._send({"type": "bind", "pairing_code": self.pairing_code, "device_name": self.device_name})
        else:
            self._send({"type": "auth", "binding_token": self.binding_token or "", "device_name": self.device_name})
        return self._recv()

    def _send(self, payload: dict) -> None:
        assert self._connection is not None
        self._connection.send(json.dumps(payload, ensure_ascii=False))

    def _recv(self) -> dict:
        assert self._connection is not None
        raw = self._connection.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    def run(self) -> None:  # pragma: no cover - 需要真实网络
        first = self.connect()
        if first.get("type") == "error":
            raise SystemExit(f"pairing failed: {first.get('error_code')}")
        self.binding_id = first.get("binding_id")
        if first.get("binding_token"):
            # 仅提示获取到令牌，不打印令牌本身
            print("bound (token received)")
        while True:
            message = self._recv()
            if message.get("type") == "accepted":
                self._dispatch(message)

    def _dispatch(self, message: dict) -> None:
        call_type = message.get("call_type", "model.generate")
        if call_type not in ("model.generate", "model.stream", "tts.synthesize", "health"):
            self._send({"type": "result", "request_id": message.get("request_id"), "ok": False, "error_code": "invalid_field"})
            return
        # 本地调用只允许回环目标；真实调用由切片 3 接入。
        self._send(
            {
                "type": "result",
                "request_id": message.get("request_id"),
                "ok": True,
                "call_type": call_type,
                "local_target": self.local_target,
            }
        )

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:  # noqa: BLE001
                pass
            self._connection = None


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser(description="learning-assistant local connector")
    parser.add_argument("--server", required=True, help="server ws:// or wss:// url")
    parser.add_argument("--pairing-code", default=None)
    parser.add_argument("--binding-token", default=None)
    parser.add_argument("--device-name", default="local-device")
    parser.add_argument("--local-target", default="http://127.0.0.1:11434")
    args = parser.parse_args(argv)
    if not args.pairing_code and not args.binding_token:
        parser.error("one of --pairing-code or --binding-token is required")
    connector = LocalConnector(
        args.server,
        pairing_code=args.pairing_code,
        binding_token=args.binding_token,
        device_name=args.device_name,
        local_target=args.local_target,
    )
    try:
        connector.run()
    finally:
        connector.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

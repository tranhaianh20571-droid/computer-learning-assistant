"""Local bindings and HTTP client for the standalone card market."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import httpx

from .slice1.crypto import CredentialCryptoError, decrypt, encrypt


class MarketError(Exception):
    def __init__(self, code: str, status: int = 502, message: str = "", detail: dict | None = None):
        super().__init__(message or code)
        self.code, self.status, self.message = code, status, message or code
        self.detail = detail or {}


def _binding_path() -> Path:
    base = os.environ.get("MATERIALS_STORAGE_DIR")
    root = Path(base) if base else Path(__file__).resolve().parents[1] / "storage"
    return root / "market_bindings.json"


class BindingStore:
    """Device tokens are encrypted with APP_ENCRYPTION_KEY and never returned to the browser."""

    _locks: dict[str, threading.RLock] = {}
    _registry_lock = threading.Lock()

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _binding_path()
        with self._registry_lock:
            self.lock = self._locks.setdefault(str(self.path.resolve()), threading.RLock())

    @contextmanager
    def guard(self):
        with self.lock:
            yield

    def _write(self, data: dict, path: Path | None = None) -> None:
        path = path or self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or any(not isinstance(item, dict) for item in data.values()):
                raise ValueError
            return data
        except (OSError, json.JSONDecodeError):
            raise MarketError("BINDING_INVALID", 409, "市集授权存储不可用，请检查本机存储后重试。") from None
        except ValueError:
            raise MarketError("BINDING_INVALID", 409, "市集授权存储不可用，请检查本机存储后重试。") from None

    def get(self, owner: str) -> dict | None:
        item = self._read().get(owner)
        if not item:
            return None
        try:
            token = decrypt(str(item.get("token", "")))
        except CredentialCryptoError:
            raise MarketError("BINDING_INVALID", 409, "市集绑定已失效，请重新绑定。") from None
        return {"market_user_id": item.get("market_user_id", ""),
                "base_url": item.get("base_url", ""), "token": token,
                "enabled": item.get("enabled", True),
                "purpose": item.get("purpose", "full"),
                "generation": item.get("generation") or hashlib.sha256(item["token"].encode()).hexdigest()}

    def save(self, owner: str, market_user_id: str, base_url: str, token: str, *, purpose: str = "full") -> None:
        with self.guard():
            data = self._read()
            data[owner] = {"market_user_id": market_user_id, "base_url": base_url,
                           "token": encrypt(token), "enabled": True, "generation": uuid.uuid4().hex,
                           "purpose": purpose}
            self._write(data)

    def clear(self, owner: str) -> None:
        with self.guard():
            data = self._read()
            data.pop(owner, None)
            self._write(data)

    def owners(self) -> list[str]:
        with self.guard():
            return list(self._read())

    def set_enabled(self, owner: str, enabled: bool) -> None:
        with self.guard():
            data = self._read()
            if owner not in data:
                raise MarketError("BIND_REQUIRED", 409, "请先授权市集账号。")
            data[owner].update(enabled=enabled, generation=uuid.uuid4().hex)
            self._write(data)

    def receive_challenge(self, code: str, *, complete_owner: str | None = None) -> dict | None:
        path = self.path.with_suffix(".pending.json")
        key = hashlib.sha256(code.encode()).hexdigest()
        with self.guard():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                data = {}
            data = {k: v for k, v in data.items() if v["expires"] > time.time()}
            if complete_owner == "":
                data[key] = {"expires": time.time() + 600}
            elif complete_owner:
                if key not in data:
                    raise MarketError("BIND_EXPIRED", 410, "授权链接已过期，请重新开启接收。")
                data[key]["owner"] = complete_owner
            if complete_owner is not None:
                self._write(data, path)
            return data.get(key)


class MarketClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def _request(self, method: str, path: str, *, timeout: float = 20, **kwargs) -> httpx.Response:
        try:
            response = httpx.request(method, self.base_url + path, timeout=timeout, **kwargs)
        except httpx.HTTPError:
            raise MarketError("MARKET_UNREACHABLE", 502, "市集暂时不可达，请稍后重试。") from None
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail") or {}
            except Exception:
                detail = {}
            raise MarketError(detail.get("error_code") or "MARKET_ERROR", response.status_code,
                              detail.get("message") or "市集操作失败。", detail)
        return response

    def login(self, email: str, password: str) -> tuple[dict, str]:
        """Sign in on the market and get a device token for this machine."""
        import httpx
        try:
            with httpx.Client(base_url=self.base_url, timeout=30) as client:
                response = client.post("/api/login", json={"email": email, "password": password})
                if response.status_code >= 400:
                    try:
                        detail = response.json().get("detail") or {}
                    except Exception:
                        detail = {}
                    raise MarketError(detail.get("error_code") or "LOGIN_FAILED",
                                      response.status_code,
                                      detail.get("message") or "邮箱或密码错误。")
                user = response.json()
                issued = client.post("/api/devices")
                if issued.status_code >= 400:
                    raise MarketError("DEVICE_FAILED", issued.status_code, "无法建立本机绑定，请稍后重试。")
                payload = issued.json()
        except httpx.HTTPError:
            raise MarketError("MARKET_UNREACHABLE", 502, "市集暂时不可达，请稍后重试。") from None
        return payload.get("user", user), payload["token"]

    def bind_status(self, code: str) -> dict:
        response = self._request("GET", "/api/bind/status", params={"code": code}, timeout=15)
        return response.json()

    def profile(self, token: str) -> dict:
        return self._account_request("GET", "/api/local/profile", token)

    def web_login(self, token: str) -> dict:
        return self._account_request("POST", "/api/web-login/tickets", token)

    def _account_request(self, method: str, path: str, token: str) -> dict:
        try:
            return self._request(method, path, headers={"Authorization": "Bearer " + token}).json()
        except MarketError as exc:
            if exc.status == 401:
                raise MarketError("BINDING_INVALID", 409, "市集授权已失效，请重新登录市集账号。") from None
            raise

    def upload(self, token: str, package: bytes, cover: tuple[bytes, str, str] | None,
               title: str, tags: list[str]) -> dict:
        files = {"package": ("card.studycard.zip", package, "application/zip")}
        if cover:
            files["cover"] = cover
        response = self._request(
            "POST", "/api/cards", timeout=180,
            headers={"Authorization": "Bearer " + token},
            files=files,
            data={"title": title, "tags": json.dumps(tags, ensure_ascii=False)},
        )
        return response.json()

    def cards(self, limit: int = 50) -> list[dict]:
        response = self._request("GET", "/api/cards", timeout=20)
        return response.json().get("data", [])[:limit]

    def pending_imports(self, token: str) -> list[dict]:
        response = self._request("GET", "/api/local/imports", timeout=20,
                                 headers={"Authorization": "Bearer " + token})
        return response.json().get("items", [])

    def report_import(self, token: str, job_id: str, status: str, message: str = "") -> None:
        self._request("POST", f"/api/local/imports/{quote(job_id)}", timeout=20,
                      headers={"Authorization": "Bearer " + token},
                      json={"status": status, "message": message})

    def download(self, token: str, card_id: str, *, timeout: float = 180,
                 cancel: threading.Event | None = None) -> bytes:
        if cancel is not None:
            deadline = time.monotonic() + 60
            output = bytearray()
            try:
                with httpx.stream("GET", self.base_url + f"/api/cards/{quote(card_id, safe='')}/package",
                                  timeout=min(timeout, 10), headers={"Authorization": "Bearer " + token}) as response:
                    if response.status_code >= 400:
                        raise MarketError("MARKET_ERROR", response.status_code, "市集下载失败，请稍后重试。")
                    for chunk in response.iter_bytes(64 * 1024):
                        if cancel.is_set() or time.monotonic() >= deadline:
                            raise MarketError("IMPORT_CANCELLED", 409, "下载已中止，请重新导入。")
                        if len(output) + len(chunk) > 128 * 1024 * 1024:
                            raise MarketError("INVALID_PACKAGE", 413, "卡片包超出限制。")
                        output.extend(chunk)
                return bytes(output)
            except httpx.HTTPError:
                raise MarketError("MARKET_UNREACHABLE", 502, "市集下载暂时不可用。") from None
        response = self._request("GET", f"/api/cards/{quote(card_id)}/package", timeout=timeout,
                                 headers={"Authorization": "Bearer " + token})
        return response.content

    def revoke(self, token: str) -> None:
        self._request("DELETE", "/api/local/authorization", timeout=15,
                      headers={"Authorization": "Bearer " + token})

    def set_reception(self, token: str, enabled: bool) -> None:
        self._request("PATCH", "/api/local/authorization", timeout=15, json={"enabled": enabled},
                      headers={"Authorization": "Bearer " + token})


def bind_start(base_url: str) -> dict:
    code = secrets.token_urlsafe(9)
    return {"code": code, "url": base_url.rstrip("/") + "/#bind=" + quote(code)}

"""Async H3/Ark wire adapters. No paid generation during capability probes."""
from __future__ import annotations

import re
import socket
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from .base import AdapterBase, AdapterError, ProbeResult, validate_endpoint
from ...net_policy import addresses_allowed, is_literal_address


def validate_video_endpoint(endpoint: str) -> None:
    if not isinstance(endpoint, str) or endpoint != endpoint.strip() or len(endpoint) > 500:
        raise AdapterError("invalid_endpoint", status=400)
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError:
        raise AdapterError("invalid_endpoint", status=400) from None
    if (parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
            or any(unicodedata.category(c).startswith("C") for c in endpoint)
            or "\\" in endpoint or port == 0):
        raise AdapterError("invalid_endpoint", status=400)
    validate_endpoint(endpoint, require_https=True, allow_private=False)
    try:
        socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except OSError:
        raise AdapterError("invalid_endpoint", status=400) from None


def video_credentials(credentials: dict) -> dict:
    if not isinstance(credentials, dict) or set(credentials) != {"api_key"}:
        raise AdapterError("credential_invalid", status=400)
    key = credentials["api_key"]
    if (not isinstance(key, str) or not key or key != key.strip() or len(key) > 4096
            or any(unicodedata.category(c).startswith("C") for c in key) or "*" in key):
        raise AdapterError("credential_invalid", status=400)
    return {"api_key": key}


def validate_video_config(endpoint: str, model_name: str, credentials: dict | None = None) -> None:
    validate_video_endpoint(endpoint)
    if (not isinstance(model_name, str) or not model_name.strip() or model_name != model_name.strip()
            or len(model_name) > 200 or any(unicodedata.category(c).startswith("C") for c in model_name)):
        raise AdapterError("invalid_field", status=400)
    if credentials is not None:
        video_credentials(credentials)


def _job_id(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise AdapterError("capability_test_failed")
    return value


@dataclass(frozen=True)
class VideoJob:
    job_id: str
    status: str
    # Temporary signed URL is business data, never an observation/event field.
    result_url: str | None = field(default=None, repr=False)
    error_code: str | None = None


class VideoGenerationAdapter(AdapterBase):
    kind = "video_generation"
    capabilities = ("video_generate",)
    default_timeout = 30.0

    @contextmanager
    def _http_client(self):
        if self._client is not None:
            yield self._client
        else:
            with httpx.Client(follow_redirects=False, trust_env=False) as client:
                yield client

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        validate_video_config(self.endpoint, self.model_name, self.credentials)

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.credentials['api_key']}", "Content-Type": "application/json"}

    def probe(self, capability: str) -> ProbeResult:
        return ProbeResult(capability, "unknown" if capability == "video_generate" else "unavailable",
                           "not_applicable")

    def _wire(self, method, path, payload=None):
        # Recheck DNS at every outbound request; never follow provider redirects.
        validate_video_endpoint(self.endpoint)
        status, body, _ = self._request(method, self.endpoint + path, json_body=payload)
        self._map_status(status, body)
        if not isinstance(body, dict) or (body.get("error") and body.get("status") != "failed"):
            raise AdapterError("capability_test_failed")
        return body

    def _request(self, method, url, *, json_body=None, params=None):
        import json
        from time import monotonic
        from .request_metrics import observe
        started, failure = monotonic(), None
        try:
            parsed = httpx.URL(url)
            infos = socket.getaddrinfo(parsed.host, parsed.port or 443, 0, socket.SOCK_STREAM)
            addresses = [item[4][0] for item in infos]
            if not addresses_allowed(addresses, from_hostname=not is_literal_address(parsed.host)):
                raise AdapterError("invalid_endpoint", status=400)
            pinned = parsed.copy_with(host=addresses[0])
            headers = {**self.headers, "Accept": "application/json", "Host": parsed.netloc.decode()}
            with self._http_client() as client:
                with client.stream(method, pinned, json=json_body, params=params, headers=headers,
                        extensions={"sni_hostname": parsed.host}, timeout=self.timeout, follow_redirects=False) as response:
                    self._map_status(response.status_code, {})
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        if len(raw) + len(chunk) > self.max_response_bytes:
                            raise AdapterError("response_too_large")
                        raw.extend(chunk)
                    return response.status_code, json.loads(raw), dict(response.headers)
        except httpx.TimeoutException:
            failure = AdapterError("timeout", status=504)
            raise failure from None
        except (httpx.HTTPError, OSError):
            failure = AdapterError("connection_error")
            raise failure from None
        except (ValueError, TypeError):
            failure = AdapterError("capability_test_failed")
            raise failure from None
        except Exception as exc:
            failure = exc
            raise
        finally:
            observe(self, started, exc=failure)

    def _payload(self, prompt, first_frame_url, duration, resolution, ratio):
        if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 7000
                or type(duration) is not int or not 4 <= duration <= 15
                or ratio not in ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9", "adaptive")):
            raise AdapterError("invalid_field", status=400)
        content = [{"type": "text", "text": prompt}]
        if first_frame_url:
            if isinstance(first_frame_url, str) and first_frame_url.startswith("data:image/png;base64,"):
                import base64
                try:
                    if len(first_frame_url) > 12 * 1024 * 1024:
                        raise ValueError()
                    base64.b64decode(first_frame_url.split(",", 1)[1], validate=True)
                except ValueError:
                    raise AdapterError("invalid_field", status=400) from None
                content.append({"type": "image_url", "image_url": {"url": first_frame_url}, "role": "first_frame"})
                return dict(model=self.model_name, content=content, duration=duration, resolution=resolution, ratio=ratio)
            # Signed reference URLs may have query parameters, but never credentials/fragments.
            parsed = urlsplit(first_frame_url)
            if parsed.username is not None or parsed.password is not None or parsed.fragment:
                raise AdapterError("invalid_endpoint", status=400)
            validate_endpoint(first_frame_url, require_https=True, allow_private=False)
            content.append({"type": "image_url", "image_url": {"url": first_frame_url}, "role": "first_frame"})
        return dict(model=self.model_name, content=content, duration=duration, resolution=resolution, ratio=ratio)

    @staticmethod
    def _result(job_id, body, url_key):
        if not isinstance(body, dict) or body.get("status") not in (
                "queued", "running", "succeeded", "failed", "cancelled", "expired"):
            raise AdapterError("capability_test_failed")
        status = body["status"]
        content = body.get("content")
        url = content.get(url_key) if isinstance(content, dict) else None
        if status == "succeeded":
            if not isinstance(url, str) or len(url) > 8192:
                raise AdapterError("capability_test_failed")
            parsed = urlsplit(url)
            if parsed.username is not None or parsed.password is not None or parsed.fragment:
                raise AdapterError("capability_test_failed")
            validate_endpoint(url, require_https=True, allow_private=False)
        else:
            url = None
        return VideoJob(job_id, status, url, "capability_test_failed" if status == "failed" else None)


class MiniMaxVideoAdapter(VideoGenerationAdapter):
    protocol = "minimax_video"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.endpoint = self.endpoint.removesuffix("/v2")

    def submit(self, prompt: str, *, first_frame_url=None, duration=5, resolution="768P", ratio="adaptive"):
        payload = self._payload(prompt, first_frame_url, duration, resolution, ratio)
        allowed = ("480P", "768P") if self.model_name == "MiniMax-H3-Max" else ("768P", "2K")
        if resolution not in allowed or (self.model_name == "MiniMax-H3-Max" and duration < 5):
            raise AdapterError("invalid_field", status=400)
        body = self._wire("POST", "/v2/video_generation", payload)
        return VideoJob(_job_id(body.get("task_id")), "queued")

    def query(self, job_id: str):
        job_id = _job_id(job_id)
        body = self._wire("GET", f"/v2/query/video_generation/{job_id}")
        task = body.get("task")
        if not isinstance(task, dict) or task.get("id") != job_id:
            raise AdapterError("capability_test_failed")
        return self._result(job_id, task, "url")

    def cancel(self, job_id: str) -> bool:
        job_id = _job_id(job_id)
        if self.query(job_id).status != "queued":
            return False
        body = self._wire("DELETE", f"/v2/video_generation/{job_id}")
        return body.get("action") == "cancelled" and body.get("status") == "cancelled"


class SeedanceVideoAdapter(VideoGenerationAdapter):
    protocol = "seedance"
    task_path = "/contents/generations/tasks"

    def submit(self, prompt: str, *, first_frame_url=None, duration=5, resolution="720p", ratio="adaptive"):
        payload = self._payload(prompt, first_frame_url, duration, resolution, ratio)
        if resolution not in ("480p", "720p", "1080p"):
            raise AdapterError("invalid_field", status=400)
        body = self._wire("POST", self.task_path, payload)
        return VideoJob(_job_id(body.get("id")), "queued")

    def query(self, job_id: str):
        job_id = _job_id(job_id)
        body = self._wire("GET", f"{self.task_path}/{job_id}")
        if body.get("id") != job_id:
            raise AdapterError("capability_test_failed")
        return self._result(job_id, body, "video_url")

    def cancel(self, job_id: str) -> bool:
        # Ark DELETE can remove a record; it is not evidence that generation stopped.
        _job_id(job_id)
        return False

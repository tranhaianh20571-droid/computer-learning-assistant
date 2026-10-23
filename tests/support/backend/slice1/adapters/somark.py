"""Fixed-destination document parser. Provider bodies never enter diagnostics."""
import json
import re

import httpx

from .base import AdapterBase, ProbeResult
from ..errors import AdapterError
from ..models import SOMARK_ENDPOINT


class SomarkAdapter(AdapterBase):
    kind = "document_parser"
    protocol = "somark"
    capabilities = ("connection",)

    def _post(self, path, *, fields=None, payload=None, kind="pdf"):
        if self.endpoint != SOMARK_ENDPOINT:
            raise AdapterError("invalid_endpoint", status=400)
        data = {"api_key": self.credentials.get("api_key", ""), **(fields or {})}
        files = {"file": ("document." + kind, payload, "application/octet-stream")} if payload is not None else None
        try:
            with self._http_client() as client:
                with client.stream("POST", self.endpoint + path, data=data, files=files,
                                   timeout=60, follow_redirects=False) as response:
                    self._map_status(response.status_code, {})
                    if not 200 <= response.status_code < 300:
                        raise AdapterError("capability_test_failed", status=502)
                    body = bytearray()
                    for part in response.iter_bytes():
                        body.extend(part)
                        if len(body) > self.max_response_bytes:
                            raise AdapterError("response_too_large", status=502)
            obj = json.loads(body)
        except httpx.TimeoutException:
            raise AdapterError("timeout", status=504) from None
        except httpx.HTTPError:
            raise AdapterError("connection_error", status=502) from None
        except (ValueError, TypeError):
            raise AdapterError("response_invalid", status=502) from None
        if not isinstance(obj, dict) or type(obj.get("code")) is not int:
            raise AdapterError("response_invalid", status=502)
        code = obj["code"]
        if code != 0:
            mapped = {1107: "credential_invalid", 1108: "quota_exceeded", 1123: "quota_exceeded",
                      1101: "rate_limited", 1124: "rate_limited", 1102: "timeout"}.get(code, "response_invalid")
            raise AdapterError(mapped, status=502)
        data = obj.get("data")
        if not isinstance(data, dict):
            raise AdapterError("response_invalid", status=502)
        return data

    def submit_document(self, payload, kind):
        data = self._post("/parse/async", payload=payload, kind=kind, fields={
            "output_formats": ["json"],
            "element_formats": json.dumps({"image": "none", "table": "html", "formula": "latex"}),
            "feature_config": json.dumps({"enable_title_level_recognition": True,
                "enable_image_ocr": True, "enable_image_understanding": True, "keep_header_footer": True}),
        })
        ident = data.get("task_id")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
            raise AdapterError("response_invalid", status=502)
        return ident

    def check_document(self, ident):
        return self._post("/parse/async_check", fields={"task_id": ident})

    def probe(self, capability):
        if capability != "connection":
            return ProbeResult(capability, "unavailable", "unsupported")
        try:
            self._post("/usage")
            return ProbeResult(capability, "available")
        except AdapterError as exc:
            return ProbeResult(capability, "unavailable",
                               exc.code if exc.code in ("credential_invalid", "timeout", "connection_error") else "capability_test_failed")

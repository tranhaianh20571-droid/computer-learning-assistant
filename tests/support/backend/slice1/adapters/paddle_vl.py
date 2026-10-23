"""Official asynchronous document OCR; signed results stay out of diagnostics."""
import json
import math
import re
import socket
from contextlib import closing
from copy import deepcopy

import httpx

from ...net_policy import addresses_allowed, is_literal_address
from ..errors import AdapterError
from ..models import PADDLE_VL_ENDPOINT, PADDLE_VL_MODEL
from .base import AdapterBase, ProbeResult

DOCUMENT_OPTIONS = {
    "useDocOrientationClassify": True, "useDocUnwarping": True,
    "useLayoutDetection": True, "useChartRecognition": True,
    "useSealRecognition": True, "useOcrForImageBlock": True,
    "layoutThreshold": 0.3, "layoutUnclipRatio": 1.06,
    "layoutMergeBboxesMode": "large",
}
LEGACY_DOCUMENT_OPTIONS = {
    "useDocOrientationClassify": False, "useDocUnwarping": False,
    "useChartRecognition": True,
}


def document_options(value=None):
    options = deepcopy(DOCUMENT_OPTIONS if value is None else value)
    if not isinstance(options, dict) or not options or set(options) - DOCUMENT_OPTIONS.keys():
        raise ValueError("invalid_document_options")
    for name, option in options.items():
        if name.startswith("use"):
            valid = type(option) is bool
        elif name == "layoutThreshold":
            valid = type(option) in (int, float) and math.isfinite(option) and 0 <= option <= 1
        elif name == "layoutUnclipRatio":
            values = option if isinstance(option, (list, tuple)) else [option, option]
            valid = len(values) == 2 and all(type(v) in (int, float) and math.isfinite(v) and v > 0 for v in values)
        else:
            valid = option in ("large", "small", "union")
        if not valid:
            raise ValueError("invalid_document_options")
    return options


def crop_document_options(options=None):
    return {**document_options(options), "useChartRecognition": False, "useDocUnwarping": False}


class PaddleVLAdapter(AdapterBase):
    kind = "document_parser"
    protocol = "paddleocr_vl"
    capabilities = ("connection",)

    def _client_or_new(self):
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False)
        return self._client

    def _wire(self, method, url, *, authenticated=True, allow_missing=False, **kwargs):
        try:
            if getattr(self, "request_guard", None):
                self.request_guard()
            parsed = httpx.URL(url)
            if authenticated:
                if self.endpoint != PADDLE_VL_ENDPOINT or not (
                        url == self.endpoint or url.startswith(self.endpoint + "/")):
                    raise AdapterError("invalid_endpoint")
            elif (parsed.scheme != "https" or parsed.username or parsed.password
                  or parsed.port not in (None, 443)
                  or not (parsed.host in ("paddleocr-store.bj.bcebos.com", "pplines-online.bj.bcebos.com") or
                          re.fullmatch(r"paddleocr-store-[0-9]+\.[a-z0-9-]+\.bcebos\.com", parsed.host))):
                raise AdapterError("invalid_endpoint")
            addresses = [item[4][0] for item in socket.getaddrinfo(
                parsed.host, parsed.port or 443, 0, socket.SOCK_STREAM)]
            if not addresses_allowed(addresses, from_hostname=not is_literal_address(parsed.host)):
                raise AdapterError("invalid_endpoint")
            headers = {"Host": parsed.netloc.decode("ascii"), "Accept": "application/json"}
            if authenticated:
                headers["Authorization"] = "bearer " + self.credentials.get("api_key", "")
            with self._http_client() as client:
                request = client.build_request(method, parsed.copy_with(host=addresses[0]), headers=headers,
                    extensions={"sni_hostname": parsed.host}, timeout=90, **kwargs)
                request.headers.pop("cookie", None)
                if not authenticated:
                    request.headers.pop("authorization", None)
                with closing(client.send(request, stream=True, follow_redirects=False)) as response:
                    if allow_missing and response.status_code == 404:
                        return None
                    self._map_status(response.status_code, {})
                    if not 200 <= response.status_code < 300:
                        raise AdapterError("response_invalid")
                    raw = bytearray()
                    for part in response.iter_bytes():
                        if len(raw) + len(part) > self.max_response_bytes:
                            raise AdapterError("response_too_large")
                        raw.extend(part)
                    return bytes(raw)
        except httpx.TimeoutException:
            raise AdapterError("timeout", status=504) from None
        except (httpx.HTTPError, OSError):
            raise AdapterError("connection_error") from None

    def _job(self, method, suffix="", **kwargs):
        raw = self._wire(method, self.endpoint + suffix, **kwargs)
        if raw is None:
            return None
        try:
            obj = json.loads(raw)
            data = obj["data"]
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except (ValueError, TypeError, KeyError):
            raise AdapterError("response_invalid") from None

    def submit_document(self, payload, kind):
        if len(payload) > 50_000_000:
            raise AdapterError("response_too_large")
        options = document_options(getattr(self, "document_options", None))
        data = self._job("POST", data={"model": PADDLE_VL_MODEL, "optionalPayload": json.dumps(options)},
            files={"file": ("document." + kind, payload, "application/octet-stream")})
        ident = data.get("jobId")
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
            raise AdapterError("response_invalid")
        return ident

    def check_document(self, ident):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", ident):
            raise AdapterError("response_invalid")
        data = self._job("GET", "/" + ident)
        state = data.get("state")
        if state in ("pending", "running"):
            return {"status": "PROCESSING"}
        if state == "failed":
            return {"status": "FAILED"}
        if state != "done":
            raise AdapterError("response_invalid")
        try:
            raw = self._wire("GET", data["resultUrl"]["jsonUrl"], authenticated=False)
            records = [json.loads(line) for line in raw.splitlines() if line.strip()]
            layouts = []
            for record in records:
                result = record.get("result")
                if not isinstance(result, dict) or not isinstance(result.get("layoutParsingResults"), list):
                    raise ValueError()
                layouts.extend(result["layoutParsingResults"])
            if not layouts or len(layouts) > 300:
                raise ValueError()
            from ...materials.document_pipeline import paddle_document_result
            return paddle_document_result(layouts,
                requested_options=document_options(getattr(self, "document_options", None)))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise AdapterError("response_invalid") from None

    def probe(self, capability):
        if capability != "connection":
            return ProbeResult(capability, "unavailable", "unsupported")
        try:
            self._job("GET", "/connection-check", allow_missing=True)
            return ProbeResult(capability, "available")
        except AdapterError as exc:
            return ProbeResult(capability, "unavailable", exc.code if exc.code in (
                "credential_invalid", "timeout", "connection_error") else "capability_test_failed")

    def download_crop(self, url):
        from ...materials.figure_crops import MAX_CROP_BYTES
        payload = self._wire("GET", url, authenticated=False)
        if not payload or len(payload) > MAX_CROP_BYTES:
            raise AdapterError("response_too_large")
        return payload

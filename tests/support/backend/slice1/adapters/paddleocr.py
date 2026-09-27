"""PaddleOCR 云端适配器骨架（切片 1 T04）。

切片 1 只交付任务提交 / 状态查询 / 结果获取接口骨架；
结构化结果与原图裁切 spike 在切片 2 执行。
"""

from __future__ import annotations

from typing import Any, Optional

from .base import AdapterBase


class PaddleOCRAdapter(AdapterBase):
    kind = "ocr"
    protocol = "paddleocr"
    capabilities = ("text", "image")
    default_timeout = 180.0

    @property
    def headers(self) -> dict[str, str]:
        key = self.credentials.get("api_key", "")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def submit(self, *, material_id: str, page_no: int, options: Optional[dict] = None) -> dict:
        payload = {"material_id": material_id, "page_no": page_no, "options": options or {}}
        status, body, _ = self._request("POST", f"{self.endpoint}/ocr/jobs", json_body=payload)
        self._map_status(status, body)
        return {"external_request_id": _extract_id(body), "status": "submitted"}

    def query(self, external_request_id: str) -> dict:
        status, body, _ = self._request("GET", f"{self.endpoint}/ocr/jobs/{external_request_id}")
        self._map_status(status, body)
        state = "unknown"
        if isinstance(body, dict):
            state = str(body.get("status") or body.get("state") or "unknown")
        return {"external_request_id": external_request_id, "status": state}

    def fetch_result(self, external_request_id: str) -> dict:
        status, body, _ = self._request("GET", f"{self.endpoint}/ocr/jobs/{external_request_id}/result")
        self._map_status(status, body)
        return {"external_request_id": external_request_id, "result": body}

    def _probe(self, capability: str) -> None:
        # 探测：提交一个合成请求；真实结构化结果验证在切片 2。
        self.submit(material_id="synthetic_probe", page_no=1)


def _extract_id(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("job_id", "task_id", "request_id", "id"):
            value = body.get(key)
            if value:
                return str(value)
    return ""

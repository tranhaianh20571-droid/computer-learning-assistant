"""ADR-022：Langfuse 提示词 REST 拉取器（不引入 Langfuse Python SDK）。

`GET {base}/api/public/v2/prompts/{name}`（Basic 认证 public:secret）。
返回 {"template": str, "version": str}；chat 型提示词拼接各 message 正文。
密钥只从环境变量读取；任何失败抛 PromptUnavailable（受控，不带原始响应体）。
真实实例联调标记为待执行验证（对端字段以部署版本为准）。
"""

from __future__ import annotations

import base64
import os
from typing import Any, Optional

import httpx

from .service import PromptUnavailable


class LangfusePromptFetcher:
    def __init__(
        self,
        base_url: Optional[str] = None,
        public_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        timeout: float = 5.0,
    ) -> None:
        self.base_url = (base_url or os.environ.get("LANGFUSE_BASE_URL") or "").rstrip("/")
        self.public_key = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY") or ""
        self.secret_key = secret_key or os.environ.get("LANGFUSE_SECRET_KEY") or ""
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.public_key and self.secret_key)

    @staticmethod
    def _prompt_to_template(payload: Any) -> str:
        prompt = payload.get("prompt") if isinstance(payload, dict) else None
        if isinstance(prompt, str):
            return prompt
        if isinstance(prompt, list):
            parts: list[str] = []
            for msg in prompt:
                if isinstance(msg, dict) and "content" in msg:
                    parts.append(str(msg["content"]))
            return "\n".join(parts)
        return ""

    def __call__(self, prompt_name: str) -> dict:
        if not self.configured:
            raise PromptUnavailable("langfuse fetcher unconfigured")
        raw = f"{self.public_key}:{self.secret_key}".encode("utf-8")
        headers = {"authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
        try:
            resp = httpx.get(
                f"{self.base_url}/api/public/v2/prompts/{prompt_name}",
                headers=headers,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise PromptUnavailable("langfuse fetch network error") from exc
        if resp.status_code != 200:
            raise PromptUnavailable(f"langfuse fetch http_{resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise PromptUnavailable("langfuse fetch invalid json") from exc
        template = self._prompt_to_template(payload)
        version = str(payload.get("version", "") if isinstance(payload, dict) else "")
        if not template or not version:
            raise PromptUnavailable("langfuse fetch incomplete")
        return {"template": template, "version": version}


__all__ = ["LangfusePromptFetcher"]

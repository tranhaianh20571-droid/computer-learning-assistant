"""Tavily Hikari 搜索适配器（切片 1 T04）。

- 统一结果格式（标题、URL、摘要、获取时间）。
- 总超时 20 秒；失败映射到受控错误码。
- 不记录查询正文；只记录 `query_id` 与结果数量。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .base import AdapterBase


class TavilyHikariAdapter(AdapterBase):
    kind = "search"
    protocol = "tavily_hikari"
    capabilities = ("text",)
    default_timeout = 20.0

    @property
    def headers(self) -> dict[str, str]:
        key = self.credentials.get("api_key", "")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        search_depth: str = "basic",
        include_domains: Optional[list[str]] = None,
    ) -> dict:
        query_id = f"qry_{uuid.uuid4().hex[:12]}"
        payload: dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
        }
        if include_domains:
            payload["include_domains"] = include_domains
        status, body, _ = self._request("POST", f"{self.endpoint}/search", json_body=payload)
        self._map_status(status, body)
        results = []
        raw_results = body.get("results") if isinstance(body, dict) else None
        fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        for item in raw_results or []:
            if not isinstance(item, dict):
                continue
            results.append(
                {
                    "title": str(item.get("title") or "")[:300],
                    "url": str(item.get("url") or "")[:2000],
                    "snippet": str(item.get("content") or item.get("snippet") or "")[:2000],
                    "fetched_at": fetched_at,
                    "source": "external_supplement",
                }
            )
        return {
            "query_id": query_id,
            "result_count": len(results),
            "results": results,
            "fetched_at": fetched_at,
        }

    def _probe(self, capability: str) -> None:
        self.search("probe", max_results=1)

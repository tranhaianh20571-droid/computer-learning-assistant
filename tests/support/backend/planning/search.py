"""网络搜索消费（T04）：真实尝试、失败等待、external 标记。

- 走切片 1 的 Tavily Hikari 适配器（统一 20 秒总截止）；
- 搜索失败 → WaitStateSignal(awaiting_search_decision)；
  用户选择重试或明确离线继续后才进入生成；
- 结果标记 `source="external"`，带获取时间与来源，与资料内容分开；
- 不伪造联网依据；查询正文不进日志（只有 query_id 与结果数量）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .errors import WaitStateSignal


@dataclass
class SearchOutcome:
    attempted: bool = True
    ok: bool = False
    query_id: str = ""
    results: list[dict] = field(default_factory=list)
    error_code: Optional[str] = None
    detail: str = ""

    @property
    def external_refs(self) -> list[dict]:
        """供 manifest 引用的 external 结果引用（不含正文快照）。"""
        return [
            {
                "query_id": self.query_id,
                "result_index": i,
                "source": "external",
                "fetched_at": r.get("fetched_at", ""),
                "domain": _domain(r.get("url", "")),
            }
            for i, r in enumerate(self.results)
        ]


def _domain(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


class SearchGate:
    """搜索真实尝试与等待决策状态机。

    adapter：具备 `search(query, ...)` 的适配器（切片 1 TavilyHikariAdapter 或
    测试注入的合成适配器）。注入 None 表示本任务无搜索配置——此时按未配置
    处理：不伪造外部结果，直接以「离线继续」语义放行并在结果中标注。
    """

    def __init__(self, adapter: Any | None = None) -> None:
        self.adapter = adapter

    def attempt(self, query: str, *, max_results: int = 5) -> SearchOutcome:
        if self.adapter is None:
            return SearchOutcome(attempted=False, detail="no_search_config")
        query_id = f"qry_{uuid.uuid4().hex[:12]}"
        try:
            raw = self.adapter.search(query, max_results=max_results)
        except Exception as exc:  # noqa: BLE001 - 适配器异常映射为受控错误码
            code = getattr(exc, "code", "") or ""
            error_code = "SEARCH_TIMEOUT" if code in ("timeout", "") else "RETRIEVAL_UNAVAILABLE"
            return SearchOutcome(
                query_id=query_id, error_code=error_code, detail=code or type(exc).__name__
            )
        results = [
            {
                "title": str(r.get("title", ""))[:300],
                "url": str(r.get("url", "")),
                "snippet": str(r.get("snippet", ""))[:2000],
                "fetched_at": str(r.get("fetched_at", "")),
                "source": "external",
            }
            for r in raw.get("results", [])
            if isinstance(r, dict)
        ]
        return SearchOutcome(query_id=str(raw.get("query_id", query_id)), ok=True, results=results)

    def require_success_or_wait(self, outcome: SearchOutcome, *, decision: Optional[str] = None) -> SearchOutcome:
        """成功或已离线放行 → 返回；失败且无决策 → 等待态。"""
        if outcome.ok or not outcome.attempted:
            return outcome
        if decision == "offline_continue":
            out = SearchOutcome(
                attempted=True, ok=False, query_id=outcome.query_id,
                error_code=outcome.error_code, detail="offline_continue",
            )
            return out
        if decision == "retry":
            # retry 由调用方重新 attempt 后再次进入本函数；此处防御性等待
            raise WaitStateSignal("awaiting_search_decision")
        raise WaitStateSignal("awaiting_search_decision")


__all__ = ["SearchGate", "SearchOutcome"]

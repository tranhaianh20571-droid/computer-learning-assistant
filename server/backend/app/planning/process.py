"""Bounded, owner-authorized planning content. Never telemetry or task events."""
from __future__ import annotations

import json
import re
import time
from contextvars import ContextVar
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import urlsplit

from sqlalchemy import select

from ..tasks.service import LearningTaskRow
from .errors import error
from .locking import planning_target
from .models import PlanningTaskStateRow, load_json_field

CURRENT_PROCESS = ContextVar("planning_process", default=None)
STAGES = (("intake", "整理学习条件"), ("coverage", "规划覆盖与前置"),
          ("retrieve", "检索学习资料"), ("search", "搜索参考来源"),
          ("generate", "生成学习规划"), ("validate", "检查规划结构"),
          ("evaluate", "体检与评审"), ("save", "保存规划草稿"))
MAX_THINKING = 8000


def clean_text(value, limit):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or ""))[:limit]


def source_view(item):
    """Only safe navigable public URLs; no opaque signatures or search excerpts."""
    try:
        parsed = urlsplit(item.url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            return None
        url = parsed._replace(fragment="").geturl()
    except ValueError:
        return None
    return {"title": clean_text(item.title, 300), "url": url[:2048],
            "domain": clean_text(parsed.hostname, 253), "used": False}


class ProcessWriter:
    def __init__(self, planning, lease):
        self.planning, self.lease = planning, lease
        self.started = time.monotonic()
        self.last_flush = 0.0
        self.bindings = {}
        self.material_revisions = None
        now = datetime.now(timezone.utc).isoformat()
        self.data = {"schema_version": 1, "task_id": lease.task_id,
            "generation": lease.generation, "attempt": lease.attempt, "sequence": 0,
            "task_status": "running", "stage": "intake", "started_at": now,
            "updated_at": now, "elapsed_ms": 0,
            "steps": [{"key": key, "label": label, "status": "pending"} for key, label in STAGES],
            "search": {"status": "pending", "query": "", "result_count": 0, "sources": [],
                       "queries": []},
            "coverage": {"status": "pending", "coverage_ratio": None, "gaps": []},
            "review": {"status": "pending", "verdict": None, "issues": []},
            "thinking": {"show_summary": True, "text": "", "available": False, "truncated": False}}
        with planning.db.session() as session:
            row = session.get(PlanningTaskStateRow, lease.task_id)
            old = load_json_field(row.process_json, {}) if row else {}
            repair = load_json_field(row.scope_signals_json, {}).get("refine_draft") if row else None
            if repair and old.get("search", {}).get("status") in ("succeeded", "empty", "failed", "skipped"):
                self.data["search"] = old["search"]
                for step in self.data["steps"]:
                    previous = next((s for s in old.get("steps", []) if s["key"] == step["key"]), None)
                    if step["key"] in ("retrieve", "search") and previous:
                        step.update(previous)
                from ..slice1.models import ServiceConfigRow
                for ident, version in old.get("_config_versions", {}).items():
                    config = session.get(ServiceConfigRow, ident)
                    if config and config.kind == "search":
                        self.bindings[ident] = version
                self.material_revisions = old.get("_material_revisions")

    def stage(self, key):
        now = datetime.now(timezone.utc).isoformat()
        for step in self.data["steps"]:
            if step["status"] == "running":
                step.update(status="succeeded", finished_at=now)
            if step["key"] == key:
                step.update(status="running", started_at=now)
        self.data["stage"] = key
        self.flush(force=True)

    def search_started(self, query):
        self.data["search"].update(status="running", query=clean_text(query, 200))
        self.stage("search")

    def search_finished(self, outcome, continuation, binding):
        from ..search.contracts import Succeeded, Empty, Failed
        if binding:
            binding.check_state()
            ident = getattr(binding, "config_id", None)
            if ident:
                self.bindings[ident] = binding.config_version
        status = ("succeeded" if isinstance(outcome, Succeeded) else "empty" if isinstance(outcome, Empty)
                  else "failed" if isinstance(outcome, Failed) else "skipped")
        sources = [source_view(item) for item in outcome.results[:10]] if isinstance(outcome, Succeeded) else []
        self.data["search"].update(status=status, sources=[s for s in sources if s],
            result_count=len(outcome.results) if isinstance(outcome, Succeeded) else 0,
            continuation=continuation.mode, reason=getattr(outcome, "reason", None),
            error_code=getattr(outcome, "error_code", None))
        for step in self.data["steps"]:
            if step["key"] == "search":
                step.update(status="failed" if status == "failed" else "skipped" if status == "skipped" else "succeeded",
                            finished_at=datetime.now(timezone.utc).isoformat())
        self.flush(force=True)

    def search_plan_finished(self, outcome, binding=None):
        """多 query 搜索的聚合快照；不记录网页正文。"""
        bindings = getattr(binding, "bindings", None) or ([binding] if binding else [])
        for item in bindings:
            item.check_state()
            ident = getattr(item, "config_id", None)
            if ident:
                self.bindings[ident] = item.config_version
        status = getattr(outcome, "state", "failed")
        self.data["search"].update(
            status=status,
            query=" | ".join(str(q.get("query", "")) for q in (outcome.queries or []))[:200],
            result_count=len(outcome.results or []),
            sources=[source_view(item) for item in (outcome.results or [])[:20] if source_view(item)],
            queries=list(outcome.queries or [])[:12],
            reason=getattr(outcome, "reason", None),
            error_code=getattr(outcome, "error_code", None),
        )
        for step in self.data["steps"]:
            if step["key"] == "search":
                step.update(status="failed" if status == "failed" else
                            "skipped" if status in ("skipped", "empty") else "succeeded",
                            finished_at=datetime.now(timezone.utc).isoformat())
        self.flush(force=True)

    def coverage_finished(self, payload: dict):
        self.data["coverage"] = payload
        self.stage("coverage")

    def review_finished(self, payload: dict):
        self.data["review"] = payload
        self.stage("evaluate")

    def bind_source(self, source):
        settings = getattr(source, "reasoning_settings", {}) or {}
        self.data["thinking"]["show_summary"] = settings.get("show_summary", True)
        if not self.data["thinking"]["show_summary"]:
            self.data["thinking"].update(text="", available=False, truncated=False)
        ident = getattr(source, "config_id", None)
        if ident:
            self.bindings[ident] = getattr(source, "config_version", None)

    def bind_deps(self, deps):
        def on_process(text, *, reset=False):
            deps.check()
            self.thinking(text, reset=reset)
        return replace(deps, on_process=on_process)

    def thinking(self, text, *, reset=False):
        block = self.data["thinking"]
        if reset:
            block.update(text="", available=False, truncated=False)
        if block["show_summary"] and text:
            content = block["text"] + clean_text(text, MAX_THINKING + 1)
            block.update(text=content[:MAX_THINKING], available=bool(content),
                         truncated=block["truncated"] or len(content) > MAX_THINKING)
        self.flush(force=reset)

    def finish(self, status):
        now = datetime.now(timezone.utc).isoformat()
        for step in self.data["steps"]:
            if step["status"] == "running":
                step.update(status="succeeded" if status in ("succeeded", "partial") else "failed", finished_at=now)
        self.data["task_status"] = status
        self.flush(force=True)

    def flush(self, *, force=False):
        now = time.monotonic()
        if not force and now - self.last_flush < 0.3:
            return
        with self.planning.db.session() as sess:
            original = sess.get(PlanningTaskStateRow, self.lease.task_id)
            if not original or original.owner_user_id != self.lease.owner_user_id:
                raise error("task_not_found", status=404)
            planning_target(sess, original.target_kind, original.target_id, original.owner_user_id, mutable=True)
            self.planning.tasks.validate_lease(self.lease, actor_id=self.lease.owner_user_id, session=sess)
            row = sess.get(PlanningTaskStateRow, self.lease.task_id, with_for_update=True, populate_existing=True)
            if row.abandoned_at:
                raise error("TASK_CANCELLED", status=409)
            if not configs_current(sess, self.bindings, row.owner_user_id):
                raise error("MODEL_CREDENTIAL_INVALID")
            materials = material_revisions(sess, row.material_ids_json, row.owner_user_id)
            if materials is None or self.material_revisions is not None and materials != self.material_revisions:
                raise error("TASK_CANCELLED", "selected material changed")
            self.material_revisions = materials
            old = load_json_field(row.process_json, {})
            self.data["sequence"] = int(old.get("sequence", 0)) + 1
            self.data["elapsed_ms"] = round((now - self.started) * 1000)
            self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
            row.process_json = json.dumps({**self.data, "_config_versions": self.bindings,
                "_material_revisions": self.material_revisions}, ensure_ascii=False)
        self.last_flush = now


def configs_current(sess, bindings, owner):
    if not bindings:
        return True
    from ..slice1.models import ServiceConfigRow
    for ident, version in bindings.items():
        config = sess.get(ServiceConfigRow, ident)
        if (not config or config.owner_scope != "user" or config.owner_user_id != owner
                or not config.is_active or config.config_version != version):
            return False
    return True


def material_revisions(sess, value, owner):
    ids = sorted(set(load_json_field(value, [])))
    if not ids:
        return []
    from ..materials.models import MaterialRow
    rows = list(sess.scalars(select(MaterialRow).where(MaterialRow.material_id.in_(ids),
        MaterialRow.owner_user_id == owner, MaterialRow.deleted_at.is_(None))))
    if len(rows) != len(ids):
        return None
    return sorted([[row.material_id, row.source_bytes_sha256] for row in rows])


def read_process(planning, task_id, owner):
    with planning.db.session() as sess:
        row = sess.get(PlanningTaskStateRow, task_id)
        task = sess.get(LearningTaskRow, task_id)
        if not row or not task or row.owner_user_id != owner or task.owner_user_id != owner:
            raise error("task_not_found", status=404)
        planning_target(sess, row.target_kind, row.target_id, owner)
        if row.abandoned_at or task.cancel_requested or task.status == "cancelled":
            return {"process": None}
        data = load_json_field(row.process_json, {})
        if data.get("generation") != task.generation or data.get("attempt") != task.attempt:
            return {"process": None}
        bindings = data.pop("_config_versions", {})
        materials = data.pop("_material_revisions", None)
        if not configs_current(sess, bindings, owner):
            return {"process": None}
        if materials is None or materials != material_revisions(sess, row.material_ids_json, owner):
            return {"process": None}
        if task.status in ("succeeded", "partial", "failed", "expired"):
            data["task_status"] = task.status
        if data.get("search", {}).get("status") == "pending" and data.get("stage") in ("generate", "validate", "save"):
            # Older repair runs overwrote their search snapshot. Recover only
            # durable counts here; never invent lost source URLs or model content.
            from ..search.repository import SearchCallRow
            call = sess.scalar(select(SearchCallRow).where(
                SearchCallRow.task_id == task_id, SearchCallRow.owner_user_id == owner
            ).order_by(SearchCallRow.logical_slot.desc()).limit(1))
            if call and call.state in ("succeeded", "empty", "failed", "skipped") and configs_current(
                    sess, {call.config_id: call.config_version}, owner):
                data["search"] = {"status": call.state, "result_count": call.result_count,
                    "sources": [], "error_code": call.error_code}
                for step in data.get("steps", []):
                    if step["key"] == "search":
                        step["status"] = "failed" if call.state == "failed" else "skipped" if call.state == "skipped" else "succeeded"
        if not data.get("thinking", {}).get("show_summary", False):
            data["thinking"] = {"show_summary": False, "text": "", "available": False}
        return {"process": data}

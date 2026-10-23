"""Server-owned personal-search execution snapshot. Never a model argument."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Callable, Literal, Mapping

from sqlalchemy import select

from ..agents.task_binding import _domain_binding
from ..materials.models import MaterialRow
from ..planning.errors import error
from ..slice1.models import ServiceConfigRow
from ..tasks.service import LearningTaskRow, Lease, TaskService
from ..logging_audit.repositories import PgTaskEventStore

ENDPOINT = "https://api.anysearch.com"


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class SearchBinding:
    owner_user_id: str
    task_id: str
    attempt_id: str
    execution_generation: int
    subject_generation: int | None
    config_id: str
    config_version: int
    endpoint: str
    deadline_at: datetime
    requirement: Literal["optional", "fresh_evidence"]
    categories: tuple[str, ...]
    scope_snapshot: Mapping[str, object]
    source_revision: str
    check_state: Callable[[], None] = field(repr=False)
    check_payload: Callable[[bytes, str, Mapping[str, object]], None] = field(repr=False)
    check_in_session: Callable[[object], None] = field(repr=False)


def bind_search(db, view, *, owner_user_id, task_id, query, requirement="optional", topic_check=None):
    """Validate task + personal config before any secret is resolved."""
    tasks = TaskService(db, PgTaskEventStore(db))
    with db.session() as sess:
        task = sess.get(LearningTaskRow, task_id)
        if not task or task.owner_user_id != owner_user_id or task.kind not in ("route_generate", "outline_generate", "planning_revise", "chat_answer","character_build_stage"):
            raise error("TASK_CANCELLED", "search task unavailable")
        if task.kind=="character_build_stage":
            from ..characters.distillation_models import CharacterBuildStageRow
            stage=sess.get(CharacterBuildStageRow,task_id)
            if not stage or not (stage.stage_key=="sources" or stage.stage_key.startswith("sources_")
                    or re.fullmatch(r"dialogue_sources_[0-9]+_[0-9]+", stage.stage_key)):
                raise error("TASK_CANCELLED","search unavailable outside source collection")
        lease = Lease(task.task_id, task.attempt_id, task.lease_token, task.lease_owner,
            task.generation, task.attempt, task.lease_until, task.deadline_at, owner_user_id)
        tasks.validate_lease(lease, actor_id=owner_user_id, session=sess)
        scope, fence, category = _domain_binding(sess, task)
        subject_generation = None
        if task.kind == "chat_answer":
            from ..planning.models import ConversationRow
            subject_generation = sess.get(ConversationRow, task.subject_id).generation
        revisions = _material_revisions(sess, scope, owner_user_id)
        # The live lease is rechecked before each wire and publication; a snapshot
        # of lease_until must not clip a window while the worker renews that lease.
        from .policy import DEFAULT_POLICY
        deadline = min(utc(value) for value in (task.deadline_at,
            datetime.now(timezone.utc) + timedelta(seconds=DEFAULT_POLICY.window_seconds)) if value)
    config_id, version = view["config_id"], view["config_version"]

    def check_in_session(sess):
        current = tasks.validate_lease(lease, actor_id=owner_user_id, session=sess)
        actual, current_fence, current_category = _domain_binding(sess, current)
        if actual != scope or current_fence != fence or current_category != category:
            raise error("TASK_CANCELLED", "search source changed")
        config = sess.get(ServiceConfigRow, config_id, with_for_update=True)
        if config is None:
            raise error("TASK_CANCELLED", "search configuration changed")
        sess.refresh(config, with_for_update=True)
        if (not config or not config.is_active or config.owner_scope != "user"
                or config.owner_user_id != owner_user_id or config.kind != "search"
                or config.protocol != "anysearch" or config.endpoint != ENDPOINT
                or config.config_version != version):
            raise error("TASK_CANCELLED", "search configuration changed")
        if _material_revisions(sess, actual, owner_user_id) != revisions:
            raise error("TASK_CANCELLED", "search material changed")
        if topic_check is not None and not topic_check(sess, query):
            raise error("TASK_CANCELLED", "search topic changed")

    def check_state():
        with db.session() as sess:
            check_in_session(sess)

    def check_payload(body, destination, actual_scope):
        from .contracts import AnySearchRequest
        check_state()
        if destination != ENDPOINT + "/v1/search" or freeze(actual_scope) != freeze(scope):
            raise error("TASK_CANCELLED", "search outbound boundary changed")
        try:
            request = AnySearchRequest.model_validate_json(body)
        except ValueError:
            raise error("TASK_CANCELLED", "search outbound contract changed") from None
        from .contracts import MAX_RESULTS
        if request.query != query or request.max_results > MAX_RESULTS:
            raise error("TASK_CANCELLED", "search query outside bound topic")

    # Random task/generation identifiers, never a query/content hash.
    source_revision = f"{lease.generation}:{subject_generation or 0}"
    binding = SearchBinding(owner_user_id, task_id, lease.attempt_id, lease.generation,
        subject_generation, config_id, version, ENDPOINT, deadline, requirement,
        (category,), freeze(scope), source_revision, check_state, check_payload, check_in_session)
    binding.check_state()
    return binding


def _material_revisions(sess, scope, owner):
    ids = scope.get("material_ids", [])
    if not ids:
        return ()
    rows = list(sess.scalars(select(MaterialRow).where(MaterialRow.material_id.in_(ids),
        MaterialRow.owner_user_id == owner, MaterialRow.deleted_at.is_(None)).with_for_update()))
    if len(rows) != len(ids):
        raise error("TASK_CANCELLED", "search material unavailable")
    return tuple(sorted((row.material_id, row.source_bytes_sha256) for row in rows))

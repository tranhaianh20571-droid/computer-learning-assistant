"""Pinned cover consent and direct image execution."""
import json
import uuid
from hashlib import sha256
from sqlalchemy import select
from .ink import InkRecipe, render_ink
from .errors import error
from .feature_models import CoverStateRow, CoverAssetRow
from .models import (CourseRow, PlanningDraftRow, RouteCourseNodeRow,
                     RouteDetachedCourseRow, load_json_field)
from ..slice1.models import ServiceConfigRow
from ..slice1.adapters.base import AdapterError

TRANSIENT_PROVIDER_CODES = frozenset(("timeout", "connection_error", "rate_limited", "provider_unavailable"))


def topic_hash(title):
    return sha256(title.encode("utf8")).hexdigest()


def description(title):
    topic = title[:200]
    motif = "一条优雅盘绕的蛇，蛇的弧线与有序的代码结构意象相呼应" if "python" in topic.lower() else "一棵分叉树，层级节点与枝干清楚表达数据结构" if any(x in topic for x in ("数据结构", "树", "算法")) else "以知识主题相关的具体物象和结构关系构图"
    return f"知识主题：{topic}。中国当代水墨课程封面，{motif}。米白宣纸，轻微纸纹，黑灰淡墨，少量青灰和淡赭，大量留白；主体居中、远离边缘，4:3横向构图，无文字、字母、水印。"


def course_topics(sess, route):
    if route.current_route_version_id:
        titles = [course.title_ref for course in sess.scalars(select(CourseRow).join(RouteCourseNodeRow,
            CourseRow.course_id == RouteCourseNodeRow.course_id).where(
            RouteCourseNodeRow.route_version_id == route.current_route_version_id,
            CourseRow.owner_user_id == route.owner_user_id, CourseRow.deleted_at.is_(None)))
            if not sess.get(RouteDetachedCourseRow, (route.route_id, course.course_id))]
    else:
        draft = sess.scalar(select(PlanningDraftRow).where(PlanningDraftRow.target_id == route.route_id,
            PlanningDraftRow.owner_user_id == route.owner_user_id, PlanningDraftRow.draft_kind == "route")
            .order_by(PlanningDraftRow.updated_at.desc(), PlanningDraftRow.draft_id.desc()).limit(1))
        content = load_json_field(draft.content_json, {}) if draft else {}
        titles = [course.get("title", "") for stage in content.get("stages", []) for course in stage.get("courses", [])]
    return [{"title": title, "title_hash": topic_hash(title)} for title in dict.fromkeys(titles) if title.strip()]


def preflight(service, kind, ident, owner):
    with service.db.session() as sess:
        target = service.target(sess, kind, ident, owner)
        from .cover_policy import resolve, suppressed
        policy = resolve(sess, kind, ident, owner)
        if not policy["cover_eligible"]:
            return {**policy, "target_kind": kind, "target_id": ident, "title": target.title_ref,
                "title_hash": topic_hash(target.title_ref), "config": None, "configs": [],
                "course_topics": [], "cover": suppressed(policy)}
        rows = sess.scalars(select(ServiceConfigRow).where(ServiceConfigRow.owner_user_id == owner,
            ServiceConfigRow.owner_scope == "user", ServiceConfigRow.kind == "image_generation",
            ServiceConfigRow.is_active.is_(True)).order_by(ServiceConfigRow.updated_at.desc())).all()
        configs = [{"config_id": row.config_id, "config_version": row.config_version,
            "model_name": row.model_name, "protocol": row.protocol, "endpoint": row.endpoint} for row in rows]
        state = sess.get(CoverStateRow, (kind, ident))
        return {**policy, "target_kind": kind, "target_id": ident, "title": target.title_ref,
            "title_hash": topic_hash(target.title_ref), "config": configs[0] if configs else None, "configs": configs,
            "course_topics": [],
            "cover": service._cover_view(sess, state) if state else {"status": "not_generated", "asset_id": None}}


def _config(sess, owner, config_id, version):
    row = sess.get(ServiceConfigRow, config_id, with_for_update=True) if config_id else None
    if (not row or not row.is_active or row.kind != "image_generation" or row.protocol != "openai_image"
            or row.owner_scope != "user" or row.owner_user_id != owner):
        raise error("config_not_found", "图片模型配置不可用", status=409)
    if version != row.config_version:
        raise error("disclosure_revoked", "图片模型配置已更新，请重新确认", status=409)
    return row


def _membership(service, sess, root_id, course_id, owner):
    route = service.target(sess, "route", root_id, owner)
    course = service.target(sess, "course", course_id, owner)
    node = sess.scalar(select(RouteCourseNodeRow).where(RouteCourseNodeRow.route_version_id == route.current_route_version_id,
        RouteCourseNodeRow.course_id == course_id).limit(1)) if route.current_route_version_id else None
    if not node or sess.get(RouteDetachedCourseRow, (root_id, course_id)):
        raise error("disclosure_required", "课程不在已确认的授权路线内", status=409)
    return course


def enqueue(service, kind, ident, owner, *, authorized=False, config_id=None, config_version=None,
            title_hash=None, scope="exact_target", authorization_scope=None, course_topic_hashes=None,
            idempotency_key=None, use_current_config=False, parent_grant_id=None, root_id=None, regenerate=False):
    scope = authorization_scope or scope
    with service.db.session() as sess:
        target = service.target(sess, kind, ident, owner)
        sess.refresh(target, with_for_update=True)
        from .cover_policy import resolve, suppressed
        policy = resolve(sess, kind, ident, owner)
        if not policy["cover_eligible"]:
            return suppressed(policy)
        if not authorized or not config_id or config_version is None or not title_hash:
            raise error("disclosure_required", "请先确认知识主题与图片模型")
        if scope not in ("exact_target", "route_and_confirmed_courses") or scope != "exact_target" and kind != "route":
            raise error("invalid_field", "封面授权范围无效")
        title = target.title_ref
        if not title.strip() or topic_hash(title) != title_hash:
            raise error("disclosure_required", "知识主题已变更，请重新确认", status=409)
        _config(sess, owner, config_id, config_version)
        if not regenerate:
            reusable = service._reusable_cover(sess, kind, ident, owner, title)
            if reusable:
                return reusable
        # Legacy broad requests are narrowed to this root; no child grants are minted.
        scope = "exact_target"
        topics = [title_hash]
        if root_id or parent_grant_id:
            raise error("disclosure_required", "子课程封面不再派生外发授权", status=409)
        snapshot = {"purpose": "course_cover", "root_kind": "route" if root_id else kind,
                    "root_id": root_id or ident, "topics": topics}
        identity = json.dumps([owner, kind, ident, title_hash, "image", config_id, config_version,
            snapshot, parent_grant_id, idempotency_key if regenerate else "default"], sort_keys=True)
        task = service.tasks.create_task(owner_user_id=owner, kind="course_cover", subject_id=ident,
            idempotency_key="cover:" + sha256(identity.encode()).hexdigest(), stage="generate",
            model_config_version=f"{config_id}:{config_version}", deadline_seconds=300, session=sess)
        asset = sess.scalar(select(CoverAssetRow).where(CoverAssetRow.task_id == task["task_id"]))
        state = sess.get(CoverStateRow, (kind, ident))
        if asset is None:
            grant_options = dict(actor_id=owner, task_id=task["task_id"], config_id=config_id,
                config_version=config_version, content_category="generated_text", scope_snapshot=snapshot, session=sess)
            grant = (service.cover_disclosures.derive(parent_grant_id=parent_grant_id, **grant_options)
                     if parent_grant_id else service.cover_disclosures.create(**grant_options))
            metadata = {"automatic": True, "title": title, "title_hash": title_hash,
                "config_id": config_id, "config_version": config_version, "grant_id": grant["grant_id"],
                "scope_snapshot": snapshot, "authorization_scope": scope, "parent_grant_id": parent_grant_id,
                "requested_method": "image", "actual_method": None, "fallback_reason": None, "error_code": None}
            asset = CoverAssetRow(asset_id=f"cover_{uuid.uuid4().hex[:16]}", task_id=task["task_id"],
                target_kind=kind, target_id=ident, owner_user_id=owner, method="image", recipe_json=json.dumps(metadata, ensure_ascii=False))
            sess.add(asset)
            if state is None:
                state = CoverStateRow(target_kind=kind, target_id=ident, owner_user_id=owner)
                sess.add(state)
            state.latest_task_id = task["task_id"]
            sess.flush()
        view = service._cover_view(sess, state)
        sess.commit()
    service.kick(task["task_id"])
    return view


def inherit(service, source_kind, source_id, kind, ident, owner):
    """Compatibility helper: completed same-topic bytes only, never an external request."""
    return reuse(service, source_kind, source_id, kind, ident, owner)


def reuse(service, source_kind, source_id, kind, ident, owner, *, session=None):
    from contextlib import nullcontext
    from .cover_policy import resolve, suppressed
    from ..tasks.service import LearningTaskRow
    with (nullcontext(session) if session is not None else service.db.session()) as sess:
        source_target = service.target(sess, source_kind, source_id, owner)
        target = service.target(sess, kind, ident, owner)
        sess.flush()
        sess.refresh(target, with_for_update=True)
        policy = resolve(sess, kind, ident, owner)
        if not policy["cover_eligible"]:
            return suppressed(policy)
        existing = service._reusable_cover(sess, kind, ident, owner, target.title_ref)
        if existing:
            return existing
        source_state = sess.get(CoverStateRow, (source_kind, source_id))
        source = sess.get(CoverAssetRow, source_state.current_asset_id) if source_state and source_state.current_asset_id else None
        metadata = load_json_field(source.recipe_json, {}) if source else {}
        source_task = sess.get(LearningTaskRow, source.task_id) if source else None
        if (not source or source.owner_user_id != owner or not source.image_bytes or not source_task
                or source_task.owner_user_id != owner or source_task.status != "succeeded"
                or source_target.title_ref != target.title_ref or metadata.get("title") != target.title_ref
                or metadata.get("title_hash") != topic_hash(target.title_ref)):
            return None
        identity = json.dumps([owner, source.asset_id, kind, ident, topic_hash(target.title_ref)])
        task = service.tasks.create_task(owner_user_id=owner, kind="course_cover", subject_id=ident,
            idempotency_key="cover-copy:" + sha256(identity.encode()).hexdigest(), stage="generate",
            deadline_seconds=300, session=sess)
        asset = sess.scalar(select(CoverAssetRow).where(CoverAssetRow.task_id == task["task_id"]))
        state = sess.get(CoverStateRow, (kind, ident))
        if asset is None:
            asset = CoverAssetRow(asset_id=f"cover_{uuid.uuid4().hex[:16]}", task_id=task["task_id"],
                target_kind=kind, target_id=ident, owner_user_id=owner, method=source.method,
                image_bytes=bytes(source.image_bytes), recipe_json=json.dumps({"title": target.title_ref,
                    "title_hash": topic_hash(target.title_ref), "reuse_asset_id": source.asset_id,
                    "method": source.method, "requested_method": metadata.get("requested_method", source.method),
                    "actual_method": source.method, "fallback_reason": None, "error_code": None}, ensure_ascii=False))
            sess.add(asset)
            if state is None:
                state = CoverStateRow(target_kind=kind, target_id=ident, owner_user_id=owner)
                sess.add(state)
            state.latest_task_id = task["task_id"]
            if service.planning.audit:
                service.planning.audit.append("config.changed", actor_id=owner, object_type="course_cover",
                    object_id=ident, result="asset_reused", detail={"source_asset_id": source.asset_id,
                        "task_id": task["task_id"]}, session=sess)
            sess.flush()
        view = service._cover_view(sess, state)
        if session is None:
            sess.commit()
    if session is None:
        service.kick(task["task_id"])
    return view

def validate(service, asset, metadata, lease, *, session=None):
    from contextlib import nullcontext
    with (nullcontext(session) if session is not None else service.db.session()) as sess:
        if service.cover_background.stop.is_set():
            raise error("TASK_CANCELLED", "封面工作进程已停止", status=409)
        service.tasks.validate_lease(lease, session=sess)
        target = service.target(sess, asset.target_kind, asset.target_id, asset.owner_user_id)
        from .cover_policy import require_eligible
        require_eligible(sess, asset.target_kind, asset.target_id, asset.owner_user_id)
        if topic_hash(target.title_ref) != metadata.get("title_hash") or target.title_ref != metadata.get("title"):
            raise error("disclosure_required", "知识主题已变更", status=409)
        state = sess.get(CoverStateRow, (asset.target_kind, asset.target_id))
        if not state or state.latest_task_id != asset.task_id:
            raise error("LEASE_LOST", "封面请求已被替换", status=409)
        snapshot = metadata.get("scope_snapshot") or {}
        if metadata.get("parent_grant_id"):
            _membership(service, sess, snapshot.get("root_id"), asset.target_id, asset.owner_user_id)
        config = _config(sess, asset.owner_user_id, metadata.get("config_id"), metadata.get("config_version"))
        service.cover_disclosures.verify_or_audit(task_id=asset.task_id, config_id=config.config_id, actor_id=asset.owner_user_id,
            content_category="generated_text", scope_snapshot={**snapshot, "topics": [metadata["title_hash"]]},
            expected_grant_id=metadata["grant_id"],
            expected_root_grant_id=metadata.get("parent_grant_id") or metadata["grant_id"], session=sess)


def draw(service, asset, metadata, lease):
    validate(service, asset, metadata, lease)
    if not service.image_adapter_factory:
        raise error("MODEL_CAPABILITY_UNAVAILABLE")
    adapter = service.image_adapter_factory(asset.owner_user_id, asset.task_id)
    if adapter is None:
        raise error("MODEL_CAPABILITY_UNAVAILABLE")
    try:
        adapter.authorize_request = lambda: validate(service, asset, metadata, lease)
        validate(service, asset, metadata, lease)
        image = adapter.generate_image(description(metadata["title"]))
        if not image:
            raise error("OUTPUT_INVALID")
        return {"image": image, "recipe": {**metadata, "method": "image", "actual_method": "image",
            "fallback": False, "fallback_reason": None, "error_code": None}}
    except AdapterError as exc:
        if exc.code not in TRANSIENT_PROVIDER_CODES:
            raise
        validate(service, asset, metadata, lease)
        recipe = InkRecipe(composition="flow" if "python" in metadata["title"].lower() else "branches")
        return {"image": render_ink(recipe, int(asset.asset_id[-6:], 16)), "recipe": {**metadata,
            "method": "procedural", "actual_method": "procedural", "fallback": True,
            "fallback_reason": exc.code, "error_code": None}}
    finally:
        adapter.close()

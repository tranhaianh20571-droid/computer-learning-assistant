"""Synthetic, isolated regression for one cover per actual library root."""
from copy import deepcopy
import json
from uuid import uuid4

from sqlalchemy import func, select

from backend.planning.feature_models import CoverAssetRow
from backend.planning.models import (CourseRow, LearningRouteRow, RouteCourseNodeRow,
    RouteDetachedCourseRow, RouteVersionRow)
from backend.planning.cover_policy import resolve
from backend.tasks.service import LearningTaskRow
from test_course_library import PAYLOAD, draft, get_library
from test_automatic_knowledge_cover import (database, env, close_synthetic_tasks, configured,
    automatic, run, confirm, get_route)


def create_draft(env, kind="route", title="Top level Python"):
    content = deepcopy(PAYLOAD if kind == "route" else PAYLOAD["stages"][0]["courses"][0])
    content["title"] = title
    return draft(env, content, "route" if kind == "route" else "course_outline")


def attach(env, rid, cid, *, historic=False):
    with env[1].db.session() as sess:
        route = sess.get(LearningRouteRow, rid)
        node = sess.scalar(select(RouteCourseNodeRow).where(
            RouteCourseNodeRow.route_version_id == route.current_route_version_id).limit(1))
        assert node is not None
        node.course_id = cid
        node.outline_version_id = sess.get(CourseRow, cid).current_outline_version_id
        if historic:
            version_id = "rv_" + uuid4().hex[:12]
            sess.add(RouteVersionRow(route_version_id=version_id, route_id=rid, version_no=2))
            sess.flush()
            route.current_route_version_id = version_id


def counts(env):
    with env[1].db.session() as sess:
        return (sess.scalar(select(func.count()).select_from(LearningTaskRow).where(
                    LearningTaskRow.owner_user_id == env[2], LearningTaskRow.kind == "course_cover")),
                sess.scalar(select(func.count()).select_from(CoverAssetRow).where(
                    CoverAssetRow.owner_user_id == env[2])))


def committed_route(env, title):
    rid, item = create_draft(env, title=title)
    response = confirm(env, item)
    assert response.status_code == 200, response.text
    return rid, item, response.json()


def test_confirm_and_repeat_confirm_create_only_one_root_cover(env):
    features, _, calls = configured(env)
    rid, item, result = committed_route(env, "Only one root")
    assert [(row["target_kind"], row["target_id"]) for row in result["covers"]] == [("route", rid)]
    before = counts(env)
    repeated = confirm(env, item)
    assert repeated.status_code == 200 and repeated.json()["deduped"] is True
    assert counts(env) == before == (1, 1)
    features._run(result["covers"][0]["task_id"])
    for stage in get_route(env, rid)["stages"]:
        for course in stage["courses"]:
            assert features.cover("course", course["course_id"], env[2])["status"] == "suppressed"
    # ADR-034 automatic mode: confirming the root plan already pinned the enabled image model, so the
    # single root cover is a real image request instead of a local placeholder.
    assert len(calls) == 1 and "无文字" in calls[0]


def test_random_request_keys_reuse_pending_current_and_explicit_regenerate(env):
    features, _, calls = configured(env)
    rid, _ = create_draft(env)
    first = automatic(env, rid, idempotency_key="random-one").json()
    second = automatic(env, rid, idempotency_key="random-two").json()
    assert first["task_id"] == second["task_id"] and counts(env) == (1, 1)
    assert len(features.cover_disclosures.list_for_task(first["task_id"], actor_id=env[2])) == 1
    assert run(env, first, rid)["status"] == "succeeded"
    current = automatic(env, rid, idempotency_key="random-three").json()
    assert current["task_id"] == first["task_id"] and len(calls) == 1
    replacement = automatic(env, rid, regenerate=True, idempotency_key="replace-now").json()
    assert replacement["task_id"] != first["task_id"] and counts(env) == (2, 2)
    assert run(env, replacement, rid)["status"] == "succeeded" and len(calls) == 2


def test_grouped_course_hides_retained_image_and_posts_do_not_enqueue(env):
    features, _, calls = configured(env)
    cid, item = create_draft(env, "course", "Independent Python")
    assert confirm(env, item).status_code == 200
    work = automatic(env, cid, kind="course", regenerate=True, idempotency_key="image").json()
    old = run(env, work, cid, "course")
    assert old["image_url"] and len(calls) == 1
    rid, _, _ = committed_route(env, "Member root")
    attach(env, rid, cid)
    before = counts(env)
    view = env[0].get(f"/api/planning/course/{cid}/cover", headers=env[3]).json()
    assert view["status"] == "suppressed" and view["cover_eligible"] is False
    assert view["cover_owner_kind"] == "route" and view["cover_owner_id"] == rid
    assert view["asset_id"] is view["task_id"] is view["image_url"] is None
    assert env[0].get(old["image_url"], headers=env[3]).status_code == 404
    for path, payload in [("cover", {"idempotency_key": "local"}), ("cover/automatic", {})]:
        response = env[0].post(f"/api/planning/course/{cid}/{path}", headers=env[3], json=payload)
        assert response.status_code == 202 and response.json()["status"] == "suppressed"
    preflight = env[0].get(f"/api/planning/course/{cid}/cover/preflight", headers=env[3]).json()
    assert preflight["cover_eligible"] is False and preflight["configs"] == []
    overview = env[0].get(f"/api/courses/{cid}/overview", headers=env[3]).json()
    assert overview["cover_owner_id"] == rid and overview["cover_eligible"] is False
    assert counts(env) == before and len(calls) == 1
    with env[1].db.session() as sess:
        assert sess.get(CoverAssetRow, old["asset_id"]).image_bytes is not None


def test_historic_membership_and_multiple_routes_match_library_until_all_detached(env):
    features, _, _ = configured(env)
    cid, _ = create_draft(env, "course", "Historical shared course")
    rid1, _, _ = committed_route(env, "Historical first root")
    rid2, _, _ = committed_route(env, "Historical second root")
    attach(env, rid1, cid, historic=True)
    attach(env, rid2, cid)
    assert cid not in {row["id"] for row in get_library(env) if row["kind"] == "course"}
    assert features.cover("course", cid, env[2])["cover_owner_id"] == min(rid1, rid2)
    with env[1].db.session() as sess:
        sess.add(RouteDetachedCourseRow(route_id=rid1, course_id=cid))
    assert features.cover("course", cid, env[2])["status"] == "suppressed"
    # ADR-038 cascade: deleting a route removes its *current* members, so rid2 is detached instead
    # of deleted here — this case is about grouping, not about cascade deletion.
    with env[1].db.session() as sess:
        sess.add(RouteDetachedCourseRow(route_id=rid2, course_id=cid))
    with env[1].db.session() as sess:
        course = sess.get(CourseRow, cid)
        course.source_route_version_id = sess.get(LearningRouteRow, rid1).current_route_version_id
        assert resolve(sess, "course", cid, env[2])["cover_eligible"] is True
    assert cid in {row["id"] for row in get_library(env) if row["kind"] == "course"}
    assert features.cover("course", cid, env[2])["status"] == "not_generated"


def test_delete_route_cascades_current_members_but_keeps_historic_ones(env):
    features, _, _ = configured(env)
    cid, _ = create_draft(env, "course", "Cascaded member")
    rid1, _, _ = committed_route(env, "Cascade first root")
    rid2, _, _ = committed_route(env, "Cascade second root")
    attach(env, rid1, cid, historic=True)
    attach(env, rid2, cid)
    assert features.cover("course", cid, env[2])["cover_owner_id"] == min(rid1, rid2)
    members = [c["course_id"] for stage in get_route(env, rid2)["stages"] for c in stage["courses"]]
    assert cid in members and len(members) >= 2
    # rid2 currently organizes these courses, so deleting rid2 deletes all of them.
    response = env[0].delete(f"/api/planning/routes/{rid2}", headers=env[3])
    assert response.status_code == 200 and sorted(response.json()["course_ids"]) == sorted(members)
    assert not {row["id"] for row in get_library(env) if row["kind"] == "course"} & set(members)
    assert env[0].get(f"/api/courses/{cid}/overview", headers=env[3]).status_code == 404
    with env[1].db.session() as sess:
        assert sess.get(CourseRow, cid).deleted_at is not None
    # The historic-only route survives deletion of the route it never currently organized.
    assert env[0].get(f"/api/routes/{rid1}/overview", headers=env[3]).status_code == 200


def test_queued_child_is_cancelled_before_supplier_call(env):
    features, _, calls = configured(env)
    cid, _ = create_draft(env, "course", "Queued child")
    work = automatic(env, cid, kind="course").json()
    rid, _, _ = committed_route(env, "Queued child root")
    attach(env, rid, cid)
    features._run(work["task_id"])
    with env[1].db.session() as sess:
        task = sess.get(LearningTaskRow, work["task_id"])
        asset = sess.scalar(select(CoverAssetRow).where(CoverAssetRow.task_id == task.task_id))
        assert task.status == "cancelled" and asset.image_bytes is None
    assert calls == []


def test_membership_change_during_image_generation_blocks_publication(env):
    features, _, calls = configured(env)
    cid, _ = create_draft(env, "course", "Racing child")
    rid, _, _ = committed_route(env, "Racing child root")
    work = automatic(env, cid, kind="course").json()
    factory = features.image_adapter_factory
    def changed_factory(actor, task_id):
        adapter = factory(actor, task_id)
        original = adapter.generate_image
        def changed(payload):
            result = original(payload)
            attach(env, rid, cid)
            return result
        adapter.generate_image = changed
        return adapter
    features.image_adapter_factory = changed_factory
    features._run(work["task_id"])
    with env[1].db.session() as sess:
        asset = sess.scalar(select(CoverAssetRow).where(CoverAssetRow.task_id == work["task_id"]))
        assert sess.get(LearningTaskRow, work["task_id"]).status == "cancelled"
        assert asset.image_bytes is None
    assert len(calls) == 1 and features.cover("course", cid, env[2])["image_url"] is None


def test_scope_conversion_copies_completed_bytes_without_new_image_call(env):
    features, _, calls = configured(env)
    rid, item = create_draft(env, title="Same topic copy")
    first = automatic(env, rid).json()
    original = run(env, first, rid)
    image = env[0].get(original["image_url"], headers=env[3]).content
    response = env[0].post(f"/api/planning/drafts/{item['draft_id']}/scope", headers=env[3],
        json={"planning_scope": "course"})
    assert response.status_code == 200, response.text
    copy = response.json()["cover"]
    cid = response.json()["draft"]["target_id"]
    assert copy["task_id"] != first["task_id"] and len(calls) == 1
    features._run(copy["task_id"])
    current = features.cover("course", cid, env[2])
    assert current["status"] == "succeeded" and current["asset_id"] != original["asset_id"]
    with env[1].db.session() as sess:
        source = sess.get(CoverAssetRow, original["asset_id"])
        target = sess.get(CoverAssetRow, current["asset_id"])
        assert source.image_bytes == target.image_bytes == image
        assert json.loads(target.recipe_json)["reuse_asset_id"] == source.asset_id
    assert env[0].delete(f"/api/planning/routes/{rid}", headers=env[3]).status_code == 200
    assert env[0].get(current["image_url"], headers=env[3]).content == image
    assert features.cover_disclosures.list_for_task(copy["task_id"], actor_id=env[2]) == []
    assert len(calls) == 1


def test_copy_survives_source_deletion_before_worker_publishes(env):
    features, _, calls = configured(env)
    rid, _ = create_draft(env, title="Copy before deletion")
    original = run(env, automatic(env, rid).json(), rid)
    cid, _ = create_draft(env, "course", "Copy before deletion")
    copied = features.reuse_cover("route", rid, "course", cid, env[2])
    assert copied and copied["task_id"] != original["task_id"]
    assert env[0].delete(f"/api/planning/routes/{rid}", headers=env[3]).status_code == 200
    features._run(copied["task_id"])
    current = features.cover("course", cid, env[2])
    assert current["status"] == "succeeded"
    assert env[0].get(current["image_url"], headers=env[3]).status_code == 200 and len(calls) == 1


def test_scope_conversion_without_completed_cover_pins_the_enabled_image_model(env):
    features, config, calls = configured(env)
    rid, item = create_draft(env, title="Local scope fallback")
    response = env[0].post(f"/api/planning/drafts/{item['draft_id']}/scope", headers=env[3],
        json={"planning_scope": "course"})
    assert response.status_code == 200, response.text
    cid = response.json()["draft"]["target_id"]
    work = response.json()["cover"]
    assert work["status"] == "queued"
    features._run(work["task_id"])
    # ADR-034 automatic mode: the enabled image model is pinned at scope conversion, no per-cover dialog.
    cover = features.cover("course", cid, env[2])
    assert cover["actual_method"] == "image" and len(calls) == 1
    grant = features.cover_disclosures.list_for_task(work["task_id"], actor_id=env[2])
    assert len(grant) == 1 and grant[0]["config_version"] == config["config_version"]
    again = features.enqueue_cover("course", cid, env[2], "new-random-key", automatic=True)
    assert again["task_id"] == work["task_id"] and len(calls) == 1


def test_scope_conversion_uses_local_cover_when_automatic_authorization_is_off(env):
    features, _, calls = configured(env)
    features.cover_automatic_authorization = False
    rid, item = create_draft(env, title="Local scope fallback off")
    response = env[0].post(f"/api/planning/drafts/{item['draft_id']}/scope", headers=env[3],
        json={"planning_scope": "course"})
    assert response.status_code == 200, response.text
    cid = response.json()["draft"]["target_id"]
    work = response.json()["cover"]
    assert work["requested_method"] == "procedural"
    features._run(work["task_id"])
    assert features.cover("course", cid, env[2])["actual_method"] == "procedural"
    again = features.enqueue_cover("course", cid, env[2], "new-random-key", automatic=True)
    assert again["task_id"] == work["task_id"] and calls == []


def test_revoked_pending_cannot_be_reused(env):
    features, _, calls = configured(env)
    rid, _ = create_draft(env, title="Invalid pinned pending")
    pending = automatic(env, rid).json()
    grant = features.cover_disclosures.list_for_task(pending["task_id"], actor_id=env[2])[0]
    features.cover_disclosures.revoke(grant["grant_id"], actor_id=env[2])
    response = automatic(env, rid, idempotency_key="another-key")
    assert response.status_code == 409 and response.json()["error_code"] == "DISCLOSURE_REVOKED"
    assert counts(env) == (1, 1) and calls == []


def test_confirm_without_image_model_keeps_the_local_cover(env):
    features, config, calls = configured(env)
    features.cover_configs.deactivate(config["config_id"], actor_id=env[2], is_admin=False)
    rid, item, result = committed_route(env, "No image model")
    assert result["covers"][0]["requested_method"] == "procedural"
    features._run(result["covers"][0]["task_id"])
    cover = features.cover("route", rid, env[2])
    assert cover["actual_method"] == "procedural" and cover["requested_method"] == "procedural"
    assert cover["image_url"] and calls == []


def test_confirm_keeps_the_local_cover_when_automatic_authorization_is_off(env):
    features, _, calls = configured(env)
    features.cover_automatic_authorization = False
    rid, item, result = committed_route(env, "Automatic authorization off")
    assert result["covers"][0]["requested_method"] == "procedural"
    features._run(result["covers"][0]["task_id"])
    assert features.cover("route", rid, env[2])["actual_method"] == "procedural" and calls == []


def test_revoked_automatic_grant_never_reaches_the_supplier(env):
    features, _, calls = configured(env)
    rid, item, result = committed_route(env, "Revoked automatic grant")
    work = result["covers"][0]
    grants = features.cover_disclosures.list_for_task(work["task_id"], actor_id=env[2])
    assert len(grants) == 1
    features.cover_disclosures.revoke(grants[0]["grant_id"], actor_id=env[2])
    features._run(work["task_id"])
    cover = features.cover("route", rid, env[2])
    assert cover["status"] == "failed" and cover["error_code"] == "DISCLOSURE_REVOKED"
    assert cover["image_url"] is None and calls == []

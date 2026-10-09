"""Synthetic isolated cover consent, fallback, hierarchy and dispatcher regressions."""
import json
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import select, func
from test_course_library import database, env, draft, confirm, get_route
from backend.slice1.config_service import ConfigService
from backend.slice1.disclosure_service import DisclosureService
from backend.slice1.models import DisclosureGrantRow, ServiceConfigRow
from backend.slice1.adapters.base import AdapterError
from backend.tasks.service import LearningTaskRow
from backend.planning.models import LearningRouteRow, utcnow
from backend.planning.feature_models import CoverAssetRow, CoverStateRow
from backend.planning.automatic_cover import description, inherit


@pytest.fixture(autouse=True)
def close_synthetic_tasks(env):
    yield
    features = env[1].features
    features.cover_background.close()
    with env[1].db.session() as sess:
        ids = list(sess.scalars(select(LearningTaskRow.task_id).where(
            LearningTaskRow.owner_user_id == env[2], LearningTaskRow.kind == "course_cover",
            LearningTaskRow.status.in_(["queued", "leased", "running"]))))
    for task_id in ids:
        features.tasks.request_cancel(task_id, actor_id=env[2])


def configured(env, failure=None):
    _, state, owner, _ = env
    features = state.features
    features.cover_configs = ConfigService(state.db, encryption_key="synthetic-cover-key")
    features.cover_disclosures = DisclosureService(state.db)
    config = features.cover_configs.create(actor_id=owner, is_admin=False, kind="image_generation", protocol="openai_image",
        endpoint="https://image.example.test/v1", model_name="synthetic-image", credentials={"api_key": "synthetic-key"})
    from backend.planning.ink import InkRecipe, render_ink
    calls = []
    class Adapter:
        def generate_image(self, prompt):
            self.authorize_request()
            calls.append(prompt)
            if failure:
                raise AdapterError(failure, status=502)
            return render_ink(InkRecipe(), 1)
        def close(self):
            pass
    features.image_adapter_factory = lambda actor, task_id: Adapter()
    features.kick = lambda task_id: None
    return features, config, calls


def automatic(env, target, kind="route", **overrides):
    view = env[0].get(f"/api/planning/{kind}/{target}/cover/preflight", headers=env[3]).json()
    config = view["config"] or {}
    body = {"external_authorized": True, "config_id": config.get("config_id"),
        "config_version": config.get("config_version"), "title_hash": view["title_hash"], **overrides}
    return env[0].post(f"/api/planning/{kind}/{target}/cover/automatic", headers=env[3], json=body)


def run(env, result, target, kind="route"):
    env[1].features._run(result["task_id"])
    return env[1].features.cover(kind, target, env[2])


def test_pinned_consent_private_image_and_idempotency(env):
    features, config, calls = configured(env)
    rid, _ = draft(env, {"title": "Python后端", "stages": []})
    response = automatic(env, rid)
    assert response.status_code == 202, response.text
    result = response.json()
    grants = features.cover_disclosures.list_for_task(result["task_id"], actor_id=env[2])
    assert len(grants) == 1 and grants[0]["config_version"] == config["config_version"]
    cover = run(env, result, rid)
    assert cover["status"] == "succeeded" and cover["actual_method"] == "image" and len(calls) == 1
    assert "蛇" in calls[0] and "无文字" in calls[0]
    assert env[0].get(cover["image_url"], headers=env[3]).status_code == 200
    assert automatic(env, rid).json()["task_id"] == result["task_id"]
    assert len(features.cover_disclosures.list_for_task(result["task_id"], actor_id=env[2])) == 1
    assert env[0].delete(f"/api/planning/routes/{rid}", headers=env[3]).status_code == 200
    assert env[0].get(cover["image_url"], headers=env[3]).status_code == 404


@pytest.mark.parametrize("body", [{"external_authorized": True}, {"external_authorized": False}])
def test_missing_explicit_pins_never_create_task_or_grant(env, body):
    features, _, calls = configured(env)
    rid, _ = draft(env)
    response = env[0].post(f"/api/planning/route/{rid}/cover/automatic", headers=env[3], json=body)
    assert response.status_code == 400
    assert features.cover("route", rid, env[2])["status"] == "not_generated" and not calls


def test_stale_topic_and_config_are_rejected(env):
    features, _, calls = configured(env)
    rid, _ = draft(env)
    assert automatic(env, rid, title_hash="0" * 64).status_code == 409
    assert automatic(env, rid, config_version=999).status_code == 409
    assert not calls and features.cover("route", rid, env[2])["status"] == "not_generated"


@pytest.mark.parametrize("code", ["timeout", "connection_error", "rate_limited", "provider_unavailable"])
def test_only_transient_provider_errors_fallback(env, code):
    features, _, calls = configured(env, code)
    rid, _ = draft(env)
    cover = run(env, automatic(env, rid).json(), rid)
    assert cover["status"] == "succeeded" and cover["requested_method"] == "image"
    assert cover["actual_method"] == "procedural" and cover["fallback_reason"] == code and len(calls) == 1


@pytest.mark.parametrize("code", ["credential_invalid", "invalid_response", "invalid_endpoint", "unsupported_capability"])
def test_security_protocol_errors_fail_closed(env, code):
    features, _, calls = configured(env, code)
    rid, _ = draft(env)
    cover = run(env, automatic(env, rid).json(), rid)
    assert cover["status"] == "failed" and cover["actual_method"] is None and cover["image_url"] is None
    assert cover["error_code"] and cover["fallback_reason"] is None and len(calls) == 1


@pytest.mark.parametrize("change", ["revoke", "config", "title", "cancel", "delete"])
def test_changed_authorization_or_target_never_sends(env, change):
    features, config, calls = configured(env)
    rid, _ = draft(env)
    result = automatic(env, rid).json()
    if change == "revoke":
        grant = features.cover_disclosures.list_for_task(result["task_id"], actor_id=env[2])[0]
        features.cover_disclosures.revoke(grant["grant_id"], actor_id=env[2])
    elif change == "cancel":
        features.tasks.request_cancel(result["task_id"], actor_id=env[2])
    elif change == "delete":
        env[0].delete(f"/api/planning/routes/{rid}", headers=env[3])
    else:
        with env[1].db.session() as sess:
            if change == "config":
                sess.get(ServiceConfigRow, config["config_id"]).config_version += 1
            else:
                sess.get(LearningRouteRow, rid).title_ref = "changed synthetic title"
            sess.commit()
    features._run(result["task_id"])
    assert not calls
    if change not in ("delete", "cancel"):
        cover = features.cover("route", rid, env[2])
        assert cover["status"] == "failed" and cover["actual_method"] is None


def test_grant_failure_rolls_back_entire_enqueue(env):
    features, _, calls = configured(env)
    rid, _ = draft(env)
    def fail(**kwargs):
        raise RuntimeError("synthetic grant failure")
    features.cover_disclosures.create = fail
    with pytest.raises(RuntimeError):
        automatic(env, rid)
    with env[1].db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(LearningTaskRow).where(LearningTaskRow.subject_id == rid)) == 0
        assert sess.get(CoverStateRow, ("route", rid)) is None
    assert not calls


def test_get_and_preflight_are_read_only(env):
    features, _, calls = configured(env)
    rid, _ = draft(env)
    result = automatic(env, rid).json()
    kicks = []
    features.kick = kicks.append
    for _ in range(3):
        assert env[0].get(f"/api/planning/route/{rid}/cover", headers=env[3]).status_code == 200
        assert env[0].get(f"/api/planning/route/{rid}/cover/preflight", headers=env[3]).status_code == 200
    assert not kicks and not calls
    assert features.tasks.get_task(result["task_id"])["status"] == "queued"


@pytest.mark.parametrize("status", ["queued", "running"])
def test_dispatcher_recovers_without_a_get(env, status):
    features, _, calls = configured(env)
    rid, _ = draft(env)
    result = automatic(env, rid).json()
    with env[1].db.session() as sess:
        row = sess.get(LearningTaskRow, result["task_id"])
        row.status = status
        if status == "running":
            row.lease_until = utcnow() - timedelta(seconds=1)
        sess.commit()
    dispatcher = features.cover_background
    for _ in range(64):
        dispatcher.tick()
        for future in list(dispatcher.pending.values()):
            future.result(timeout=10)
        if features.tasks.get_task(result["task_id"])["status"] == "succeeded":
            break
    else:
        pytest.fail("Dispatcher did not recover the target within 64 FIFO batches")
    dispatcher.close()
    assert features.cover("route", rid, env[2])["actual_method"] == "image" and len(calls) == 1


def test_total_deadline_is_terminal_not_retried(env):
    features, _, calls = configured(env)
    rid, _ = draft(env)
    result = automatic(env, rid).json()
    with env[1].db.session() as sess:
        sess.get(LearningTaskRow, result["task_id"]).deadline_at = utcnow() - timedelta(seconds=1)
        sess.commit()
    features.cover_background.tick()
    assert features.tasks.get_task(result["task_id"])["status"] == "expired"
    assert not features.cover_background.pending and not calls
    features.cover_background.close()


def test_procedural_upgrade_preserves_existing_image_on_fallback(env):
    features, _, calls = configured(env, "timeout")
    rid, _ = draft(env)
    old = features.enqueue_cover("route", rid, env[2], "local", "procedural")
    features._run(old["task_id"])
    previous = features.cover("route", rid, env[2])
    new = automatic(env, rid, regenerate=True, idempotency_key="explicit-upgrade").json()
    assert new["task_id"] != old["task_id"] and new["asset_id"] == previous["asset_id"]
    cover = run(env, new, rid)
    assert cover["asset_id"] == previous["asset_id"] and cover["actual_method"] == "procedural"
    assert len(calls) == 1


def test_legacy_broad_scope_is_narrowed_to_one_root(env):
    features, _, calls = configured(env)
    rid, item = draft(env)
    root = automatic(env, rid, authorization_scope="route_and_confirmed_courses").json()
    response = confirm(env, item)
    assert response.status_code == 200, response.text
    assert [(row["target_kind"], row["target_id"]) for row in response.json()["covers"]] == [("route", rid)]
    assert response.json()["covers"][0]["task_id"] == root["task_id"]
    cid = get_route(env, rid)["stages"][0]["courses"][0]["course_id"]
    child = inherit(features, "route", rid, "course", cid, env[2])
    assert child["status"] == "suppressed" and child["task_id"] is None
    grants = features.cover_disclosures.list_for_task(root["task_id"], actor_id=env[2])
    assert len(grants) == 1
    assert grants[0]["scope_snapshot"]["topics"] == [features.cover_preflight("route", rid, env[2])["title_hash"]]
    features.cover_disclosures.revoke(grants[0]["grant_id"], actor_id=env[2])
    cover = run(env, root, rid)
    assert cover["status"] == "failed" and not calls


def test_new_direct_grant_cannot_bypass_revoked_pinned_root(env):
    features, config, calls = configured(env)
    rid, _ = draft(env)
    root = automatic(env, rid).json()
    with env[1].db.session() as sess:
        asset = sess.scalar(select(CoverAssetRow).where(CoverAssetRow.task_id == root["task_id"]))
        metadata = json.loads(asset.recipe_json)
    features.cover_disclosures.revoke(metadata["grant_id"], actor_id=env[2])
    replacement = features.cover_disclosures.create(actor_id=env[2], task_id=root["task_id"],
        config_id=config["config_id"], config_version=config["config_version"], content_category="generated_text",
        scope_snapshot=metadata["scope_snapshot"])
    assert replacement["grant_id"] != metadata["grant_id"] and replacement["parent_grant_id"] is None
    retry = env[0].post(f"/api/planning/covers/{root['task_id']}/run", headers=env[3])
    assert retry.status_code == 409 and retry.json()["error_code"] == "DISCLOSURE_REVOKED"
    cover = run(env, root, rid)
    assert cover["status"] == "failed" and cover["error_code"] == "DISCLOSURE_REVOKED"
    assert cover["image_url"] is None and cover["actual_method"] is None and not calls


def test_exact_scope_does_not_inherit_unrelated_course(env):
    features, _, calls = configured(env)
    rid, _ = draft(env)
    automatic(env, rid)
    cid, _ = draft(env, {"title": "Synthetic unrelated", "units": []}, "course_outline")
    assert inherit(features, "route", rid, "course", cid, env[2]) is None
    assert features.cover("course", cid, env[2])["status"] == "not_generated" and not calls


def test_concurrent_same_request_has_one_task_and_grant(env):
    features, config, _ = configured(env)
    rid, _ = draft(env)
    view = features.cover_preflight("route", rid, env[2])
    def enqueue(_):
        return features.enqueue_automatic_cover("route", rid, env[2], authorized=True,
            config_id=config["config_id"], config_version=config["config_version"], title_hash=view["title_hash"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue, range(2)))
    assert results[0]["task_id"] == results[1]["task_id"]
    assert len(features.cover_disclosures.list_for_task(results[0]["task_id"], actor_id=env[2])) == 1


def test_topic_motifs():
    assert "蛇" in description("Python后端") and "分叉树" in description("数据结构与算法")


@pytest.mark.parametrize("change", ["revoke", "delete", "title", "lease"])
def test_change_during_generation_cannot_publish(env, change):
    features, _, calls = configured(env)
    rid, _ = draft(env)
    result = automatic(env, rid).json()
    original = features.image_adapter_factory
    def factory(actor, task_id):
        instance = original(actor, task_id)
        generate = instance.generate_image
        def changed(prompt):
            image = generate(prompt)
            if change == "revoke":
                grant = features.cover_disclosures.list_for_task(task_id, actor_id=actor)[0]
                features.cover_disclosures.revoke(grant["grant_id"], actor_id=actor)
            elif change == "delete":
                env[0].delete(f"/api/planning/routes/{rid}", headers=env[3])
            else:
                with env[1].db.session() as sess:
                    if change == "title":
                        sess.get(LearningRouteRow, rid).title_ref = "changed during synthetic generation"
                    else:
                        sess.get(LearningTaskRow, task_id).lease_token = "synthetic-new-token"
                    sess.commit()
            return image
        instance.generate_image = changed
        return instance
    features.image_adapter_factory = factory
    features._run(result["task_id"])
    assert len(calls) == 1
    with env[1].db.session() as sess:
        asset = sess.scalar(select(CoverAssetRow).where(CoverAssetRow.task_id == result["task_id"]))
        assert asset is None or asset.image_bytes is None


def test_retry_success_clears_previous_error_metadata(env):
    features, _, calls = configured(env, "invalid_response")
    rid, _ = draft(env)
    result = automatic(env, rid).json()
    failed = run(env, result, rid)
    assert failed["status"] == "failed" and failed["error_code"]
    from backend.planning.ink import InkRecipe, render_ink
    class Recovered:
        def generate_image(self, prompt):
            self.authorize_request()
            calls.append(prompt)
            return render_ink(InkRecipe(), 2)
        def close(self):
            pass
    features.image_adapter_factory = lambda actor, task_id: Recovered()
    retry = env[0].post(f"/api/planning/covers/{result['task_id']}/run", headers=env[3])
    assert retry.status_code == 202, retry.text
    cover = run(env, result, rid)
    assert cover["status"] == "succeeded" and cover["error_code"] is None and cover["actual_method"] == "image"
    assert len(calls) == 2


def test_close_is_bounded_and_restart_waits_for_old_worker(env):
    from threading import Event
    from time import monotonic
    features, _, _ = configured(env)
    rid, _ = draft(env)
    automatic(env, rid)
    background = features.cover_background
    entered, release = Event(), Event()
    def blocked(task_id):
        entered.set()
        release.wait(5)
    features._run = blocked
    background.tick()
    assert entered.wait(2)
    futures = list(background.pending.values())
    try:
        before = monotonic()
        background.close()
        assert monotonic() - before < 1
        with pytest.raises(RuntimeError, match="still stopping"):
            background.start()
        assert background.stop.is_set()
    finally:
        release.set()
        for future in futures:
            future.result(timeout=2)


def test_dispatcher_stop_prevents_late_image_publication(env):
    features, _, calls = configured(env)
    rid, _ = draft(env)
    result = automatic(env, rid).json()
    factory = features.image_adapter_factory
    def stopped_factory(actor, task_id):
        instance = factory(actor, task_id)
        instance.close = features.cover_background.close
        return instance
    features.image_adapter_factory = stopped_factory
    cover = run(env, result, rid)
    assert len(calls) == 1 and cover["status"] == "cancelled" and cover["image_url"] is None


def test_live_lease_is_not_reclaimed_by_dispatcher(env):
    features, _, calls = configured(env)
    rid, _ = draft(env)
    result = automatic(env, rid).json()
    features.tasks.claim(result["task_id"], lease_owner="synthetic-live-worker", lease_seconds=120)
    features.cover_background.tick()
    assert not features.cover_background.pending and not calls

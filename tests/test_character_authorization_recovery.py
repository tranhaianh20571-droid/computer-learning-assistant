"""Character consent recovery uses the current config and immutable persona."""
from datetime import timedelta
import hashlib
import json
import uuid

import pytest
from sqlalchemy import select

from backend.characters.models import CharacterSegmentRow
from backend.planning.models import utcnow
from backend.slice1.errors import Slice1Error
from backend.slice1.models import DisclosureGrantRow, ServiceConfigRow
from backend.tasks.service import LearningTaskRow
from backend.teaching.errors import TeachingError
from test_character_authorization_transaction import bound, env, grant  # noqa: F401
from test_native_teaching_authorization import approve


def switch_config(env, *, version=False):
    with env.db.session() as sess:
        old = sess.get(ServiceConfigRow, env.content)
        if version:
            old.config_version += 1
        else:
            old.is_active = False
            ident = "content_" + uuid.uuid4().hex[:10]
            sess.add(ServiceConfigRow(config_id=ident, owner_user_id=env.owner,
                kind="content_model", protocol="openai",
                endpoint="https://replacement.example.test/v1", model_name="synthetic"))
            env.content = ident

    def describe(owner, kind):
        with env.db.session() as sess:
            row = sess.get(ServiceConfigRow, env.content if kind == "content_model" else env.voice)
            return {"config_id": row.config_id, "config_version": row.config_version,
                "protocol": row.protocol} if owner == env.owner and row.is_active else None
    env.service.configs.latest_active_for_owner = describe


def character_grants(env, task_id):
    with env.db.session() as sess:
        return list(sess.scalars(select(DisclosureGrantRow).where(
            DisclosureGrantRow.task_id == task_id,
            DisclosureGrantRow.content_category == "character_media").order_by(DisclosureGrantRow.granted_at)))


@pytest.mark.parametrize("version", [False, True])
def test_recovery_replaces_old_config_grant_without_changing_frozen_persona(bound, version):
    env, start, card = bound
    old = grant(env, start, card)
    env.service.disclosures.revoke(old["grant_id"], actor_id=env.owner)
    with env.db.session() as sess:
        frozen_before = sess.get(CharacterSegmentRow, (start["conversation_id"], 1)).profile_json
    switch_config(env, version=version)
    assert env.service.run_step_generation(start["task_id"])["status"] == "failed"
    assert len(character_grants(env, start["task_id"])) == 1
    recover = approve(env, purpose="recover", conversation_id=start["conversation_id"])
    assert recover["task_id"] == start["task_id"] and recover["status"] == "queued"
    env.service._check_authorization(start["task_id"])
    assert env.service.run_step_generation(start["task_id"])["status"] == "succeeded"
    grants = character_grants(env, start["task_id"])
    assert len(grants) == 2
    assert grants[0].revoked_at and grants[0].grant_id == old["grant_id"]
    assert grants[1].config_id == env.content
    assert grants[1].config_version == (2 if version else 1)
    assert json.loads(grants[1].scope_snapshot)["card_id"] == card["card_id"]
    with env.db.session() as sess:
        assert sess.get(CharacterSegmentRow, (start["conversation_id"], 1)).profile_json == frozen_before


def test_current_character_revocation_cannot_be_overridden_by_fresh_grant(bound):
    env, start, card = bound
    revoked = grant(env, start, card)
    env.service.disclosures.revoke(revoked["grant_id"], actor_id=env.owner)
    grant(env, start, card)
    for action in (lambda: env.service._check_authorization(start["task_id"]),
                   lambda: approve(env, purpose="recover", conversation_id=start["conversation_id"])):
        with pytest.raises((Slice1Error, TeachingError)) as caught:
            action()
        assert caught.value.code.lower() == "disclosure_revoked"
    assert env.calls == []
    assert len(character_grants(env, start["task_id"])) == 2


@pytest.mark.parametrize("field,value", [
    ("card_id", "synthetic-other-card"), ("revision", 99), ("persona_hash", "0" * 64)])
def test_current_config_grant_must_match_the_entire_frozen_persona(bound, field, value):
    env, start, card = bound
    scope = {"card_id": card["card_id"], "revision": card["revision"],
        "persona_hash": hashlib.sha256(json.dumps(card["profile"],
            sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    scope[field] = value
    env.service.disclosures.create(actor_id=env.owner, task_id=start["task_id"],
        config_id=env.content, content_category="character_media", scope_snapshot=scope)
    with pytest.raises(TeachingError) as caught:
        env.service._check_authorization(start["task_id"])
    assert caught.value.code == "DISCLOSURE_REQUIRED"
    assert env.calls == []
    approve(env, purpose="recover", conversation_id=start["conversation_id"])
    env.service._check_authorization(start["task_id"])
    assert len(character_grants(env, start["task_id"])) == 2


def test_missing_grant_is_readonly_until_explicit_recovery(bound):
    env, start, _ = bound
    with pytest.raises(TeachingError) as caught:
        env.service._check_authorization(start["task_id"])
    assert caught.value.code == "DISCLOSURE_REQUIRED"
    assert character_grants(env, start["task_id"]) == []
    assert env.calls == []
    approve(env, purpose="recover", conversation_id=start["conversation_id"])
    approve(env, purpose="recover", conversation_id=start["conversation_id"])
    assert len(character_grants(env, start["task_id"])) == 1
    env.service._check_authorization(start["task_id"])


def test_expired_character_grant_is_renewed_only_by_explicit_recovery(bound):
    env, start, card = bound
    old = grant(env, start, card)
    with env.db.session() as sess:
        sess.get(DisclosureGrantRow, old["grant_id"]).expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(Slice1Error) as caught:
        env.service._check_authorization(start["task_id"])
    assert caught.value.code == "disclosure_expired"
    assert len(character_grants(env, start["task_id"])) == 1
    approve(env, purpose="recover", conversation_id=start["conversation_id"])
    assert len(character_grants(env, start["task_id"])) == 2
    env.service._check_authorization(start["task_id"])


def test_recovery_rolls_back_config_binding_and_all_grants_together(bound, monkeypatch):
    env, start, card = bound
    grant(env, start, card)
    with env.db.session() as sess:
        before = sess.get(LearningTaskRow, start["task_id"]).model_config_version
        before_ids = set(sess.scalars(select(DisclosureGrantRow.grant_id).where(
            DisclosureGrantRow.owner_user_id == env.owner)))
    switch_config(env)
    original = env.service._character_disclosure

    def fail_after_prepare(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("prepare"):
            raise RuntimeError("synthetic recovery rollback")
        return result

    monkeypatch.setattr(env.service, "_character_disclosure", fail_after_prepare)
    with pytest.raises(RuntimeError, match="synthetic recovery rollback"):
        approve(env, purpose="recover", conversation_id=start["conversation_id"])
    with env.db.session() as sess:
        task = sess.get(LearningTaskRow, start["task_id"])
        assert task.model_config_version == before
        assert task.status == "queued"
        assert set(sess.scalars(select(DisclosureGrantRow.grant_id).where(
            DisclosureGrantRow.owner_user_id == env.owner))) == before_ids


def test_recovery_preserves_succeeded_step_binding_and_artifact(bound):
    env, start, card = bound
    grant(env, start, card)
    assert env.service.run_step_generation(start["task_id"])["status"] == "succeeded"
    before = env.service.get_workspace(start["conversation_id"], actor_id=env.owner)["steps"][0]
    with env.db.session() as sess:
        binding = sess.get(LearningTaskRow, start["task_id"]).model_config_version
    switch_config(env)
    approve(env, purpose="recover", conversation_id=start["conversation_id"])
    after = env.service.get_workspace(start["conversation_id"], actor_id=env.owner)["steps"][0]
    assert before["text"] == after["text"]
    assert before["character"] == after["character"]
    with env.db.session() as sess:
        assert sess.get(LearningTaskRow, start["task_id"]).model_config_version == binding
        assert sess.get(LearningTaskRow, start["task_id"]).status == "succeeded"
    assert len(character_grants(env, start["task_id"])) == 1


def test_missing_artifact_repair_prepares_character_grant_on_new_task(bound):
    env, start, card = bound
    grant(env, start, card)
    lease = env.service.tasks.claim(start["task_id"], lease_owner="synthetic-corruption")
    env.service.tasks.submit_result(lease, stage="generate", status="succeeded")
    recovered = approve(env, purpose="recover", conversation_id=start["conversation_id"])
    assert recovered["task_id"] != start["task_id"]
    assert len(character_grants(env, recovered["task_id"])) == 1
    result = env.service.run_step_generation(recovered["task_id"])
    assert result["status"] == "succeeded", result
    with env.db.session() as sess:
        assert sess.get(LearningTaskRow, start["task_id"]).status == "succeeded"


def test_missing_artifact_repair_keeps_current_persona_revocation_fence(bound):
    env, start, card = bound
    created = grant(env, start, card)
    lease = env.service.tasks.claim(start["task_id"], lease_owner="synthetic-corruption")
    env.service.tasks.submit_result(lease, stage="generate", status="succeeded")
    env.service.disclosures.revoke(created["grant_id"], actor_id=env.owner)
    with pytest.raises((Slice1Error, TeachingError)) as caught:
        approve(env, purpose="recover", conversation_id=start["conversation_id"])
    assert caught.value.code.lower() == "disclosure_revoked"
    assert env.calls == []

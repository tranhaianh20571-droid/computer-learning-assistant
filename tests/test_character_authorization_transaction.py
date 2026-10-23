"""PostgreSQL regression for nested character authorization self-locks."""
import hashlib
import json

import pytest
from sqlalchemy import event

from backend.characters.models import CharacterCardRow
from backend.characters.schemas import Profile
from backend.characters.service import CharacterService, segment_character
from backend.planning.models import utcnow
from backend.slice1.errors import Slice1Error
from backend.slice1.models import DisclosureGrantRow, ServiceConfigRow
from backend.teaching.errors import TeachingError
from test_native_teaching_authorization import env, approve  # noqa: F401


@pytest.fixture
def bound(env):
    def bounded_lock(connection, record, proxy):
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '750ms'")

    event.listen(env.db.engine, "checkout", bounded_lock)
    start = approve(env)
    cards = CharacterService(env.db, None, None, None)
    card = cards.create(env.owner, Profile(name="Synthetic transaction tutor"))
    cards.bind(env.owner, start["conversation_id"], card["card_id"], 1, 1)
    with env.db.session() as sess:
        segment_character(sess, start["conversation_id"], 1, freeze=True)
    return env, start, card


def grant(env, start, card, session=None):
    scope = {"card_id": card["card_id"], "revision": card["revision"],
        "persona_hash": hashlib.sha256(json.dumps(card["profile"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    return env.service.disclosures.create(actor_id=env.owner, task_id=start["task_id"],
        config_id=env.content, content_category="character_media",
        scope_snapshot=scope, session=session)


def test_uncommitted_character_grant_is_checked_without_committing_outer_transaction(bound):
    env, start, card = bound
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with env.db.session() as sess:
            created = grant(env, start, card, sess)
            env.service._check_authorization(start["task_id"], session=sess)
            with env.db.session() as other:
                assert other.get(DisclosureGrantRow, created["grant_id"]) is None
            raise RuntimeError("synthetic rollback")
    with env.db.session() as sess:
        assert sess.get(DisclosureGrantRow, created["grant_id"]) is None


@pytest.mark.parametrize("mutation", ["revoke", "delete", "version"])
def test_character_transaction_keeps_revocation_deletion_and_version_fences(bound, mutation):
    env, start, card = bound
    created = grant(env, start, card)
    with env.db.session() as sess:
        if mutation == "revoke":
            sess.get(DisclosureGrantRow, created["grant_id"]).revoked_at = utcnow()
        elif mutation == "delete":
            sess.get(CharacterCardRow, card["card_id"]).deleted_at = utcnow()
        else:
            sess.get(ServiceConfigRow, env.content).config_version += 1
    with pytest.raises((Slice1Error, TeachingError)) as caught:
        env.service._check_authorization(start["task_id"])
    assert getattr(caught.value, "code", "").lower() in {
        "disclosure_revoked", "disclosure_required"}


def test_bound_character_can_publish_with_real_postgres_authorization(bound):
    env, start, card = bound
    grant(env, start, card)
    assert env.service.run_step_generation(start["task_id"])["status"] == "succeeded"
    workspace = env.service.get_workspace(start["conversation_id"], actor_id=env.owner)
    assert workspace["steps"][0]["character"]["card_id"] == card["card_id"]
    assert workspace["steps"][0]["text"]

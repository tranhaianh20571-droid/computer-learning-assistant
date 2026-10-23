"""Synthetic shared defaults, explicit personal choice and immutable lessons."""
import uuid
import json
import pytest
from sqlalchemy import delete
from backend.characters.defaults import install_system_default
from backend.characters.models import CharacterSystemDefaultRow,CharacterBindingRow
from backend.characters.schemas import Profile,UseMentor
from backend.characters.persona import PersonaPackage
from test_character_cards import env,image  # noqa: F401
from test_course_library import env as classroom_env,database  # noqa: F401
from test_native_migrations import engine as migration_engine,migration  # noqa: F401


def test_system_default_migration_matches_model_and_preserves_nonempty(migration_engine):
    from sqlalchemy import inspect,text
    with migration_engine.begin() as conn:
        conn.execute(text("CREATE TABLE character_cards (card_id varchar(64) PRIMARY KEY)"))
        script=migration("a1f2g3h4i5j6_system_mentor.py",conn)
        script.upgrade()
        actual={column["name"]:column for column in inspect(conn).get_columns("character_system_default")}
        assert set(actual)==set(CharacterSystemDefaultRow.__table__.columns.keys())
        for column in CharacterSystemDefaultRow.__table__.columns:
            assert actual[column.name]["nullable"]==column.nullable
            assert str(actual[column.name]["type"].compile(dialect=conn.dialect))==str(column.type.compile(dialect=conn.dialect))
        conn.execute(text("INSERT INTO character_cards VALUES ('synthetic-card')"))
        conn.execute(text("INSERT INTO character_system_default (key,card_id,revision) VALUES ('system','synthetic-card',1)"))
        assert conn.scalar(text("SELECT version FROM character_system_default"))==1
        with pytest.raises(RuntimeError,match="nonempty"):
            script.downgrade()
        conn.execute(text("DELETE FROM character_system_default"))
        script.downgrade()
        assert "character_system_default" not in inspect(conn).get_table_names()


def complete_card(cards,owner):
    card=cards.create(owner,Profile(name="Synthetic public tutor",appearance="A blue cotton shirt."))
    card=cards.edit_persona(owner,card["card_id"],1,PersonaPackage(
        skill="# Synthetic public reasoning\nCheck necessary conditions before drawing conclusions."))
    assets=[cards.add_asset(owner,card["card_id"],card["revision"],image(),"image","image/png",scene,confirmed=True)
        for scene in ("neutral","explaining","thinking","encouraging","celebrating")]
    data=Profile.model_validate({**card["profile"],"media_ids":assets,"base_asset_id":assets[0],
        "pointing_rig":None})
    card=cards.edit(owner,card["card_id"],card["revision"],data)
    cards.publish(owner,card["card_id"],card["revision"])
    return card


def test_every_new_user_gets_published_default_but_personal_choice_wins(env):
    cards,_=env;owner="synthetic-default-"+uuid.uuid4().hex
    card=complete_card(cards,owner)
    try:
        install_system_default(cards,owner,card["card_id"],card["revision"])
        for learner in ("synthetic-new-a","synthetic-new-b"):
            visible=cards.list(learner)
            assert len(visible)==1 and visible[0]["system_default"] and not visible[0]["editable"]
            assert cards.preference(learner)["using_system_default"]
            assert cards.collect(learner,card["card_id"],False)=={"collected":True}
        personal=cards.create("synthetic-new-a",Profile(name="Private choice"))
        cards.use("synthetic-new-a",UseMentor(expected_version=0,card_id=personal["card_id"],revision=1))
        assert cards.preference("synthetic-new-a")["card_id"]==personal["card_id"]
        cards.use("synthetic-new-a",UseMentor(expected_version=1))
        assert cards.preference("synthetic-new-a")["card_id"]==card["card_id"]
        assert cards.preference("synthetic-new-b")["card_id"]==card["card_id"]
        cards.delete(owner,card["card_id"])
        assert cards.list("synthetic-new-b")==[]
    finally:
        with cards.db.session() as sess:sess.execute(delete(CharacterSystemDefaultRow))


def test_incomplete_card_cannot_become_system_default(env):
    from backend.slice1.errors import Slice1Error
    cards,_=env;owner="synthetic-incomplete-"+uuid.uuid4().hex
    card=cards.create(owner,Profile(name="Incomplete"))
    cards.publish(owner,card["card_id"],1)
    with pytest.raises(Slice1Error):
        install_system_default(cards,owner,card["card_id"],1)


def test_updating_default_publishes_same_card_atomically_and_rolls_back_on_audit_failure(env,monkeypatch):
    from backend.characters.defaults import system_default
    cards,_=env;owner="synthetic-atomic-"+uuid.uuid4().hex
    card=complete_card(cards,owner)
    try:
        install_system_default(cards,owner,card["card_id"],card["revision"])
        revised=cards.edit(owner,card["card_id"],card["revision"],
            Profile.model_validate({**card["profile"],"name":"Synthetic revised tutor"}))
        append=cards.audit.append
        def fail(event,**kwargs):
            if kwargs.get("object_type")=="system_mentor" and kwargs.get("result")=="selected":
                raise RuntimeError("synthetic audit failure")
            return append(event,**kwargs)
        monkeypatch.setattr(cards.audit,"append",fail)
        with pytest.raises(RuntimeError,match="synthetic audit failure"):
            install_system_default(cards,owner,card["card_id"],revised["revision"],publish=True)
        assert cards.get(owner,card["card_id"])["published_revision"]==card["revision"]
        with cards.db.session() as sess:
            assert system_default(sess).revision==card["revision"]
        monkeypatch.setattr(cards.audit,"append",append)
        install_system_default(cards,owner,card["card_id"],revised["revision"],publish=True)
        assert cards.get(owner,card["card_id"])["published_revision"]==revised["revision"]
        with cards.db.session() as sess:
            assert system_default(sess).revision==revised["revision"]
    finally:
        with cards.db.session() as sess:sess.execute(delete(CharacterSystemDefaultRow))


def test_default_is_bound_with_complete_skill_in_new_lesson(tmp_path,classroom_env):
    from test_teaching_assessments import lesson
    from test_classroom_mentor_policy import cards as make_cards
    classroom=classroom_env
    cards=make_cards(classroom,tmp_path)
    try:
        author="synthetic-author-"+uuid.uuid4().hex
        card=complete_card(cards,author)
        install_system_default(cards,author,card["card_id"],card["revision"])
        _,_,start=lesson(classroom)
        with cards.db.session() as sess:
            bound=sess.get(CharacterBindingRow,start["conversation_id"])
            assert bound.card_id==card["card_id"]
            assert json.loads(bound.profile_json)["_persona"]["skill"]==card["persona"]["skill"]
    finally:
        with cards.db.session() as sess:sess.execute(delete(CharacterSystemDefaultRow))

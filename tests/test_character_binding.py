"""Frozen classroom identity and asset visibility across publishing revisions."""
import io
import pytest
from PIL import Image
from backend.characters.service import CharacterService, segment_character
from backend.characters.schemas import Profile
from backend.materials.storage import MaterialStorage
from backend.slice1.config_service import ConfigService
from backend.slice1.errors import Slice1Error
from test_teaching_assessments import env, database, lesson  # noqa: F401


def test_bound_segment_keeps_old_assets_and_rejects_other_owner_or_delete(env, tmp_path):
    client, state, owner, headers = env
    _, _, start = lesson(env)
    cards = CharacterService(state.db, ConfigService(state.db, encryption_key="synthetic-card-binding", audit=state.audit),
        state.audit, MaterialStorage(tmp_path))
    card = cards.create(owner, Profile(name="Synthetic mentor"))
    buffer = io.BytesIO()
    Image.new("RGBA", (80, 120), (20, 40, 30, 80)).save(buffer, format="PNG")
    asset = cards.add_asset(owner, card["card_id"], 1, buffer.getvalue(), "image", "image/png", confirmed=True)
    old = cards.edit(owner, card["card_id"], 1, Profile(name="Synthetic mentor", media_ids=[asset], base_asset_id=asset))
    cards.publish(owner, card["card_id"], old["revision"])
    cards.bind(owner, start["conversation_id"], card["card_id"], old["revision"], 1)
    assert client.post(f"/api/teaching/tasks/{start['task_id']}/run", headers=headers).json()["status"] == "succeeded"
    cards.edit(owner, card["card_id"], old["revision"], Profile(name="Changed mentor"))
    cards.publish(owner, card["card_id"], old["revision"]+1)
    with state.db.session() as sess:
        frozen = segment_character(sess, start["conversation_id"], 1)
        assert frozen["revision"] == old["revision"] and frozen["profile"]["base_asset_id"] == asset
    assert cards.segment_assets(owner, start["conversation_id"], 1, asset)[0] == buffer.getvalue()
    with pytest.raises(Slice1Error): cards.segment_assets("synthetic-other", start["conversation_id"], 1, asset)
    with pytest.raises(Slice1Error): cards.bind(owner, start["conversation_id"], card["card_id"], old["revision"]+1, 1)
    cards.delete(owner, card["card_id"])
    with pytest.raises(Slice1Error): cards.segment_assets(owner, start["conversation_id"], 1, asset)

"""Portable card packages: private media, sanitized voice fields, safe import."""
import io
import json
import uuid

import pytest
from PIL import Image

from backend.characters.package import export_package, import_package
from backend.characters.persona import CharacterPersonaRow, PersonaPackage
from backend.characters.schemas import Profile
from backend.characters.service import CharacterService
from backend.logging_audit.db import Database
from backend.logging_audit.repositories import PgAuditLog
from backend.materials.storage import MaterialStorage
from backend.slice1.config_service import ConfigService
from backend.slice1.errors import Slice1Error


def image():
    output = io.BytesIO()
    Image.new("RGBA", (48, 64), (20, 40, 30, 200)).save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def cards(tmp_path):
    db = Database()
    db.create_all()
    audit = PgAuditLog(db)
    configs = ConfigService(db, encryption_key="synthetic-package-key", audit=audit)
    service = CharacterService(db, configs, audit, MaterialStorage(tmp_path))
    try:
        yield service
    finally:
        db.dispose()


def _published_package(cards, owner):
    card = cards.create(owner, Profile(name="Synthetic package tutor", personality="Calm",
                                       tags=["synthetic"]))
    asset_id = cards.add_asset(owner, card["card_id"], 1, image(), "image", "image/png",
                               "neutral", confirmed=True)
    edited = cards.edit(owner, card["card_id"], 1, Profile(
        name="Synthetic package tutor", personality="Calm", tags=["synthetic"],
        media_ids=[asset_id], base_asset_id=asset_id))
    with cards.db.session() as sess:
        sess.add(CharacterPersonaRow(card_id=card["card_id"], revision=edited["revision"],
                                     payload_json=PersonaPackage(skill="Synthetic skill").model_dump_json()))
    return card["card_id"], edited["revision"]


def test_export_import_round_trip_sanitizes_voice(cards):
    owner = "synthetic-package-" + uuid.uuid4().hex
    other = "synthetic-import-" + uuid.uuid4().hex
    card_id, revision = _published_package(cards, owner)

    payload = export_package(cards, owner, card_id)
    assert payload[:2] == b"PK"

    imported = import_package(cards, other, payload)
    assert imported["card_id"] != card_id
    assert imported["revision"] == 2
    assert imported["profile"]["name"] == "Synthetic package tutor"
    assert imported["profile"]["voice_id"] == ""
    assert imported["profile"]["voice_config_id"] == ""
    assert len(imported["profile"]["media_ids"]) == 1
    assert imported["profile"]["base_asset_id"] == imported["profile"]["media_ids"][0]
    assert imported["persona"]["skill"] == "Synthetic skill"

    source_asset = cards.get(owner, card_id)["profile"]["media_ids"][0]
    source_bytes, _ = cards.asset(owner, card_id, source_asset)
    target_bytes, _ = cards.asset(other, imported["card_id"], imported["profile"]["media_ids"][0])
    assert source_bytes == target_bytes


def test_import_rejects_zip_slip(cards):
    owner = "synthetic-package-" + uuid.uuid4().hex
    output = io.BytesIO()
    with __import__("zipfile").ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", json.dumps({
            "format": "studypal.card/1",
            "profile": {"name": "Synthetic"},
            "assets": [],
        }))
        archive.writestr("../escape.txt", "synthetic")
    with pytest.raises(Slice1Error) as caught:
        import_package(cards, owner, output.getvalue())
    assert caught.value.status == 422

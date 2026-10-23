"""Portable character-card packages.

A package is a ZIP with one manifest and private media. Import creates a new
local card owned by the importing account; provider credentials, voice choices
and publication state are never carried across accounts.
"""
from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import PurePosixPath

from sqlalchemy import delete, select

from .models import CharacterAssetRow, CharacterCardRow, CharacterRevisionRow
from .persona import CharacterPersonaRow, PersonaPackage, read_persona
from .schemas import Profile
from .service import reject

FORMAT = "studypal.card/1"
MAX_PACKAGE_BYTES = 128 * 1024 * 1024
MAX_ASSET_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 64
ALLOWED_KINDS = {"image", "video"}
ALLOWED_SCENES = {"neutral", "explaining", "thinking", "encouraging", "celebrating"}
ALLOWED_MIME = {"image/png", "image/jpeg", "image/webp", "video/mp4"}


def _sanitized_profile(profile: dict) -> dict:
    clean = dict(profile)
    clean["voice_id"] = ""
    clean["voice_config_id"] = ""
    clean["voice_selection"] = "auto"
    return clean


def export_package(cards, owner: str, card_id: str) -> bytes:
    with cards.db.session() as sess:
        card = cards._card(sess, card_id, owner)
        revision = sess.get(CharacterRevisionRow, (card_id, card.current_revision))
        if not revision:
            reject()
        profile = _sanitized_profile(json.loads(revision.profile_json))
        persona = read_persona(sess, card_id, card.current_revision)
        rows = sess.scalars(select(CharacterAssetRow).where(
            CharacterAssetRow.card_id == card_id,
            CharacterAssetRow.asset_id.in_(profile.get("media_ids", [])),
            CharacterAssetRow.confirmed.is_(True),
        )).all()
        assets = [(row, cards.storage.get(row.storage_key)) for row in rows]

    manifest_assets = []
    for row, payload in assets:
        manifest_assets.append({
            "asset_id": row.asset_id,
            "kind": row.kind,
            "scene": row.scene,
            "mime": row.mime_type,
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "path": f"assets/{row.asset_id}",
        })
    # Content hash ignores timestamps so re-exporting the same revision dedupes.
    content = {
        "format": FORMAT,
        "source": {"card_id": card_id, "revision": card.current_revision},
        "profile": profile,
        "persona": persona,
        "assets": manifest_assets,
    }
    content_sha = hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = {
        "format": FORMAT,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "content_sha256": content_sha,
        **{key: value for key, value in content.items() if key != "format"},
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        for row, payload in assets:
            archive.writestr(f"assets/{row.asset_id}", payload)
    return output.getvalue()


def _read_package(raw: bytes):
    if not raw or len(raw) > MAX_PACKAGE_BYTES:
        reject("invalid_field", 413, message="卡片包大小超出限制。")
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        reject("invalid_field", 422, message="卡片包不是有效的 ZIP 文件。")
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        reject("invalid_field", 422, message="卡片包条目过多。")
    total = 0
    names = set()
    contents = {}
    for info in infos:
        if info.is_dir():
            continue
        if info.flag_bits & 0x1:
            reject("invalid_field", 422, message="卡片包不能加密。")
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in PurePosixPath(name).parts:
            reject("invalid_field", 422, message="卡片包路径无效。")
        if name in names:
            reject("invalid_field", 422, message="卡片包条目重复。")
        names.add(name)
        total += info.file_size
        if info.file_size > MAX_ASSET_BYTES or total > MAX_PACKAGE_BYTES:
            reject("invalid_field", 413, message="卡片包解压后超出限制。")
        contents[name] = archive.read(info)
    if "manifest.json" not in contents:
        reject("invalid_field", 422, message="卡片包缺少 manifest.json。")
    try:
        manifest = json.loads(contents["manifest.json"])
    except (UnicodeDecodeError, json.JSONDecodeError):
        reject("invalid_field", 422, message="卡片包 manifest 格式无效。")
    if manifest.get("format") != FORMAT:
        reject("invalid_field", 422, message="不支持的卡片包版本。")
    return manifest, contents


def _cleanup(cards, card_id: str | None, keys: list[str]) -> None:
    for key in keys:
        try:
            cards.storage.delete(key)
        except Exception:
            pass
    if not card_id:
        return
    with cards.db.session() as sess:
        sess.execute(delete(CharacterPersonaRow).where(CharacterPersonaRow.card_id == card_id))
        sess.execute(delete(CharacterAssetRow).where(CharacterAssetRow.card_id == card_id))
        sess.execute(delete(CharacterRevisionRow).where(CharacterRevisionRow.card_id == card_id))
        sess.execute(delete(CharacterCardRow).where(CharacterCardRow.card_id == card_id))


def import_package(cards, owner: str, raw: bytes) -> dict:
    manifest, contents = _read_package(raw)
    profile_data = manifest.get("profile")
    if not isinstance(profile_data, dict):
        reject("invalid_field", 422, message="卡片包缺少人物设定。")
    asset_entries = manifest.get("assets")
    if not isinstance(asset_entries, list):
        reject("invalid_field", 422, message="卡片包缺少素材清单。")

    profile = _sanitized_profile(profile_data)
    media_ids = profile.get("media_ids", [])
    if not isinstance(media_ids, list):
        reject("invalid_field", 422, message="人物素材列表无效。")
    listed = {}
    for entry in asset_entries:
        asset_id = entry.get("asset_id") if isinstance(entry, dict) else None
        if not isinstance(asset_id, str) or not asset_id:
            reject("invalid_field", 422, message="素材标识无效。")
        if entry.get("kind") not in ALLOWED_KINDS or entry.get("scene") not in ALLOWED_SCENES:
            reject("invalid_field", 422, message="素材类型无效。")
        if entry.get("mime") not in ALLOWED_MIME:
            reject("invalid_field", 422, message="素材格式无效。")
        path = entry.get("path")
        if path != f"assets/{asset_id}" or path not in contents:
            reject("invalid_field", 422, message="素材文件缺失。")
        payload = contents[path]
        if len(payload) != entry.get("size") or hashlib.sha256(payload).hexdigest() != entry.get("sha256"):
            reject("invalid_field", 422, message="素材校验失败。")
        listed[asset_id] = (entry, payload)
    missing = [asset_id for asset_id in media_ids if asset_id not in listed]
    if missing:
        reject("invalid_field", 422, message="人物素材不完整。")

    card_id = None
    keys: list[str] = []
    try:
        stub = dict(profile)
        stub.update(media_ids=[], base_asset_id="", pointing_rig=None, voice_id="",
                    voice_config_id="", voice_selection="auto")
        created = cards.create(owner, Profile.model_validate(stub))
        card_id = created["card_id"]
        mapping = {}
        for asset_id in media_ids:
            entry, payload = listed[asset_id]
            new_id = cards.add_asset(owner, card_id, 1, payload, entry["kind"], entry["mime"],
                                     entry["scene"], confirmed=True)
            mapping[asset_id] = new_id
            asset = None
            with cards.db.session() as sess:
                asset = sess.get(CharacterAssetRow, new_id)
            if asset:
                keys.append(asset.storage_key)
        mapped = dict(profile)
        mapped["media_ids"] = [mapping[asset_id] for asset_id in media_ids]
        base = profile.get("base_asset_id")
        mapped["base_asset_id"] = mapping.get(base, "")
        rig = profile.get("pointing_rig")
        if isinstance(rig, dict):
            mapped_rig = dict(rig)
            for key in ("upper_arm_asset_id", "forearm_asset_id", "body_asset_id", "reference_asset_id"):
                if mapped_rig.get(key):
                    mapped_rig[key] = mapping.get(mapped_rig[key], "")
            mapped["pointing_rig"] = mapped_rig
        else:
            mapped["pointing_rig"] = None
        updated = cards.edit(owner, card_id, 1, Profile.model_validate(mapped))
        persona = manifest.get("persona")
        if persona:
            package = PersonaPackage.model_validate(persona)
            with cards.db.session() as sess:
                sess.add(CharacterPersonaRow(card_id=card_id, revision=updated["revision"],
                                             payload_json=package.model_dump_json()))
        return cards.get(owner, card_id)
    except Exception:
        _cleanup(cards, card_id, keys)
        raise

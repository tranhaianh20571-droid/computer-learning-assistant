"""Private provider crops with fixed identities; never persist signed URLs."""
from __future__ import annotations

import hashlib
import io
import json
import re
import warnings

from .storage import StorageError

MAX_CROP_BYTES = 12_000_000
MAX_CROP_PIXELS = 16_000_000
MAX_BATCH_CROP_BYTES = 128_000_000


def owned_pdf_state(session, owner, material_id):
    from .source_models import source_records
    rows = source_records(session, owner, [material_id], parsed=True)
    if len(rows) != 1:
        raise StorageError("object_not_found")
    material, row = rows[0]
    state = json.loads(row.result_json)
    if material.kind != "pdf" and not (
            material.kind == "file" and state.get("document_input", {}).get("kind") == "pdf"):
        raise StorageError("object_not_found")
    return material, row, state


def matched_crop(images, bbox):
    if not isinstance(images, dict) or not isinstance(bbox, list) or len(bbox) != 4:
        return None
    found = []
    for path, url in images.items():
        if not isinstance(path, str) or not isinstance(url, str):
            continue
        match = re.fullmatch(r"(?:imgs/)?img_in_(?:image|chart|footer_image)_box_(\d+)_(\d+)_(\d+)_(\d+)\.(?:jpg|jpeg|png)", path)
        if match and [int(v) for v in match.groups()] == bbox:
            found.append(url)
    return found[0] if len(found) == 1 else None


def crop_key(material_id, page_no, block_no, crop):
    sha = crop.get("sha256", "") if isinstance(crop, dict) else ""
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", material_id)
            or type(page_no) is not int or page_no < 1
            or type(block_no) is not int or block_no < 0
            or not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha)):
        raise StorageError("invalid_object_key")
    return f"fig/{material_id}/p{page_no}_b{block_no}_{sha}.png"


def png_crop(payload):
    from PIL import Image
    if not payload or len(payload) > MAX_CROP_BYTES:
        raise ValueError("crop_size_invalid")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload)) as image:
                if image.format not in ("PNG", "JPEG", "WEBP") or image.width * image.height > MAX_CROP_PIXELS:
                    raise ValueError("crop_image_invalid")
                image.load()
                output = io.BytesIO()
                # Preserve pixels and geometry, without resizing or enhancement.
                image.convert("RGB").save(output, "PNG")
                data = output.getvalue()
                if len(data) > MAX_CROP_BYTES:
                    raise ValueError("crop_size_invalid")
                return data, {"sha256": hashlib.sha256(data).hexdigest(),
                              "width": image.width, "height": image.height}
    except (OSError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("crop_image_invalid") from None


def load_crop(storage, material_id, page_no, block_no, crop):
    payload = storage.get(crop_key(material_id, page_no, block_no, crop))
    if len(payload) > MAX_CROP_BYTES or hashlib.sha256(payload).hexdigest() != crop["sha256"]:
        raise StorageError("object_content_changed")
    return payload


def persist_crops(state, storage, material_id, adapter, guard):
    total = 0
    for page in state["page_results"]:
        for number, block in enumerate(page["blocks"]):
            url = block.pop("provider_crop_url", None)
            if not url:
                continue
            guard()
            if not material_id:
                block["crop_status"] = "unavailable"
                continue
            if total >= MAX_BATCH_CROP_BYTES:
                block["crop_status"] = "unavailable"
                continue
            payload = adapter.download_crop(url)
            guard()
            try:
                data, crop = png_crop(payload)
                total += len(data)
                if total > MAX_BATCH_CROP_BYTES:
                    raise ValueError("crop_batch_size_invalid")
            except ValueError:
                block["crop_status"] = "unavailable"
                continue
            storage.put(crop_key(material_id, page["page_no"], number, crop), data)
            guard()
            if block.get("crop_match_bbox"):
                crop["matched_bbox"] = block.pop("crop_match_bbox")
            block.update(crop=crop, crop_status="available")


def visual_crops(storage, material_id, window, guard):
    images, size = [], 0
    for block in window["source_blocks"]:
        if not block.get("crop") or block["type"] != "figure":
            continue
        if len(images) >= 12:
            break
        guard()
        number = int(block["block_id"].rsplit("/b", 1)[1])
        try:
            payload = load_crop(storage, material_id, block["page_no"], number, block["crop"])
        except StorageError:
            continue
        if size + len(payload) > 24_000_000:
            continue
        size += len(payload)
        images.append((block["block_id"], payload))
    return images


def crop_storage_bytes(storage, material_id, state):
    keys = {crop_key(material_id, page["page_no"], number, block["crop"])
            for page in state.get("page_results", []) for number, block in enumerate(page["blocks"])
            if block.get("crop")}
    return sum(storage.local_path(key).stat().st_size for key in keys)

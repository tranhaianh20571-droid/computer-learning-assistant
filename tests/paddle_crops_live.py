"""Reuse a private Paddle response to fetch crops without repeating paid OCR."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--pages", type=int, nargs="+", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = (root / args.out).resolve()
    if not folder.is_relative_to((root / ".backup").resolve()):
        parser.error("Private output must remain below .backup.")
    folder.mkdir(parents=True, exist_ok=True)
    raw_path, scope_path = (root / args.raw).resolve(), (root / args.scope).resolve()
    raw_bytes = raw_path.read_bytes()
    scope = json.loads(scope_path.read_text(encoding="utf8"))
    pages = sorted(set(args.pages))
    if not set(pages) <= set(scope["original_pages"]):
        parser.error("Selected pages must belong to the recorded response scope.")
    progress_path = folder / "run.json"
    run = json.loads(progress_path.read_text(encoding="utf8")) if progress_path.exists() else {}
    signature = {"response_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                 "scope_sha256": hashlib.sha256(scope_path.read_bytes()).hexdigest(),
                 "original_pages": pages}
    if run and any(run[k] != value for k, value in signature.items()):
        parser.error("Existing experiment scope changed.")
    if run.get("failure"):
        parser.error("Prior failure requires diagnosis; do not repeat automatically.")
    sys.path.insert(0, str(root / "server/backend"))
    os.environ.update(CONTENT_TRACE_ENABLED="0", OTEL_ENABLED="0", PYDANTIC_AI_NO_BANNER="1")
    from app.logging_audit.sdk_logging import install_sdk_logging_safety
    from app.materials.document_pipeline import normalize_result, paddle_document_result
    from app.materials.figure_crops import (
        crop_key,
        load_crop,
        persist_crops,
        visual_crops,
    )
    from app.materials.question_extraction import blocks_and_anchors
    from app.materials.storage import MaterialStorage
    from app.slice1.adapters.paddle_vl import DOCUMENT_OPTIONS, PaddleVLAdapter
    from app.slice1.models import PADDLE_VL_ENDPOINT
    install_sdk_logging_safety()
    storage = MaterialStorage(folder / "storage")
    material_id = "synthetic-private-crop-probe"
    state_path = folder / "document-private.json"
    if run.get("status") != "completed":
        layouts = [layout for line in raw_bytes.splitlines() if line.strip()
                   for layout in json.loads(line)["result"]["layoutParsingResults"]]
        if len(layouts) != len(scope["original_pages"]):
            parser.error("Response and scope page counts differ.")
        state = {"pages": max(scope["original_pages"]), "page_results": []}
        for original, layout in zip(scope["original_pages"], layouts, strict=True):
            if original in pages:
                normalized = normalize_result(paddle_document_result([layout], requested_options=DOCUMENT_OPTIONS),
                                              expected_pages=1, page_offset=original - 1)
                state["page_results"].extend(normalized["page_results"])
        run = {**signature, "status": "processing"}
        progress_path.write_text(json.dumps(run, indent=2), encoding="utf8")
        started = time.monotonic()
        adapter = PaddleVLAdapter(endpoint=PADDLE_VL_ENDPOINT, credentials={}, max_response_bytes=12_000_000)
        try:
            persist_crops(state, storage, material_id, adapter, lambda: None)
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf8")
            run.update(status="completed", seconds=round(time.monotonic() - started, 2))
        except Exception as exc:  # noqa: BLE001 - signed URLs never enter diagnostics
            run["failure"] = {"type": type(exc).__name__, "code": getattr(exc, "code", "CROP_PROBE_FAILED")}
            print(json.dumps(run["failure"]), flush=True)
            return 1
        finally:
            adapter.close()
            progress_path.write_text(json.dumps(run, indent=2), encoding="utf8")
    state = json.loads(state_path.read_text(encoding="utf8"))
    blocks, _ = blocks_and_anchors(state)
    visual = visual_crops(storage, material_id, {"source_blocks": blocks}, lambda: None)
    records = []
    for page in state["page_results"]:
        figures = []
        for number, block in enumerate(page["blocks"]):
            if block["type"] != "figure":
                continue
            crop = block.get("crop")
            if crop:
                load_crop(storage, material_id, page["page_no"], number, crop)
            figures.append({"block_no": number, "crop": crop,
                            "local_key": crop_key(material_id, page["page_no"], number, crop) if crop else None})
        records.append({"original_page": page["page_no"], "figures": figures})
    result = {"status": "completed", "ocr_submissions": 0, "credential_used": False,
              "pages": records, "actual_visual_inputs": len(visual),
              "download_seconds": run["seconds"]}
    (folder / "review-private.json").write_text(json.dumps(result, indent=2), encoding="utf8")
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

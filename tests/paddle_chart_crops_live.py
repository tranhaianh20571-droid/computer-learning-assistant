"""Replay a completed crop pass, fetching its image assets without new OCR."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from threading import Event


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--upload", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--fresh-crop-pass", action="store_true",
                        help="Submit only a new pinned crop pass, reusing completed main OCR.")
    parser.add_argument("--supplement-run", help="Replay a separately captured crop pass without new OCR.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = (root / args.out).resolve()
    upload = (root / args.upload).resolve(strict=True)
    if not folder.is_relative_to((root / ".backup").resolve()):
        parser.error("Private output must remain below .backup.")
    folder.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root / "server/backend"))
    os.environ.update(CONTENT_TRACE_ENABLED="0", OTEL_ENABLED="0", PYDANTIC_AI_NO_BANNER="1")
    from app.logging_audit.sdk_logging import install_sdk_logging_safety
    from app.materials.chart_crops import complete_chart_crops
    from app.materials.document_pipeline import paddle_document_result
    from app.materials.storage import MaterialStorage
    from app.slice1.adapters.paddle_vl import PaddleVLAdapter, crop_document_options
    from app.slice1.models import PADDLE_VL_ENDPOINT
    install_sdk_logging_safety()
    run = json.loads((upload / "run.json").read_text(encoding="utf8"))
    source = upload.parent / "problem-pages.pdf"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if digest != run["documents"][0]["sha256"]:
        parser.error("Main OCR and input PDF differ.")
    scope = {"sample_sha256": digest, "source_config_id": run["source_config_id"],
             "source_version": run["source_version"], "fresh_crop_pass": args.fresh_crop_pass}
    scope_path = folder / "scope.json"
    if scope_path.exists() and json.loads(scope_path.read_text(encoding="utf8")) != scope:
        parser.error("Existing experiment scope changed.")
    scope_path.write_text(json.dumps(scope, indent=2), encoding="utf8")
    state = json.loads((upload / "document-1-private.json").read_text(encoding="utf8"))
    raw_folder = upload
    if args.supplement_run:
        raw_folder = (root / args.supplement_run).resolve(strict=True)
        supplemental_state = json.loads((raw_folder / "document-private.json").read_text(encoding="utf8"))
        state["batches"] = supplemental_state["batches"]
        state["ocr_crop_options"] = supplemental_state["ocr_crop_options"]
    output_state = folder / "document-private.json"
    if args.fresh_crop_pass and output_state.exists():
        state = json.loads(output_state.read_text(encoding="utf8"))
    batch = state["batches"][0]
    if not args.fresh_crop_pass and (not batch.get("crop_job_id") or batch.get("crop_status") != "completed"):
        parser.error("A completed supplemental job is required.")
    layouts = []
    for path in raw_folder.glob("provider-result-*.jsonl"):
        for line in path.read_bytes().splitlines():
            for layout in json.loads(line)["result"]["layoutParsingResults"]:
                if layout["prunedResult"].get("model_settings", {}).get("use_chart_recognition") is False:
                    layouts.append(layout)
    if not args.fresh_crop_pass and len(layouts) != len(batch["crop_pages"]):
        parser.error("Supplemental response scope is ambiguous.")
    class Replay(PaddleVLAdapter):
        def submit_document(self, *args):
            raise AssertionError("A replay must not submit paid OCR.")

        def check_document(self, ident):
            if ident != batch["crop_job_id"]:
                raise ValueError("Unexpected job.")
            return paddle_document_result(layouts, requested_options=state["ocr_crop_options"])
    guard = lambda: None
    if args.fresh_crop_pass:
        if not output_state.exists():
            state["ocr_crop_options"] = crop_document_options(state["ocr_options"])
            batch = {k: v for k, v in batch.items() if not k.startswith("crop_") and k != "standalone_chart_crops"}
            state["batches"] = [batch]
            output_state.write_text(json.dumps(state, ensure_ascii=False), encoding="utf8")
        from dotenv import load_dotenv
        load_dotenv(root / "server/.env")
        from app.logging_audit.db import Database
        from app.slice1.config_service import ConfigService
        from app.slice1.models import ServiceConfigRow
        configs = ConfigService(Database())
        with configs.db.session() as session:
            row = session.get(ServiceConfigRow, run["source_config_id"])
            owner = row.owner_user_id
        def guard():
            view = configs.get(run["source_config_id"], actor_id=owner, is_admin=False)
            if not view["is_active"] or view["config_version"] != run["source_version"]:
                raise ValueError("Source configuration changed.")
        class Fresh(PaddleVLAdapter):
            def _wire(self, *values, **options):
                guard()
                raw = super()._wire(*values, **options)
                if options.get("authenticated") is False and raw and raw.lstrip().startswith(b"{"):
                    (folder / ("provider-result-" + hashlib.sha256(raw).hexdigest()[:16] + ".jsonl")).write_bytes(raw)
                return raw
        guard()
        adapter = Fresh(endpoint=PADDLE_VL_ENDPOINT, credentials=configs.resolve_document_credentials(
            run["source_config_id"], owner_user_id=owner, expected_version=run["source_version"]),
            max_response_bytes=12_000_000)
    else:
        adapter = Replay(endpoint=PADDLE_VL_ENDPOINT, credentials={}, max_response_bytes=12_000_000)
    adapter.document_options = state["ocr_options"]
    normalized = {"page_results": state["page_results"]}
    try:
        complete_chart_crops(normalized, state, batch, source=source,
            storage=MaterialStorage(upload / "private-storage"), material_id=run["documents"][0]["material_id"],
            adapter=adapter, guard=guard,
            checkpoint=lambda _: output_state.write_text(json.dumps(state, ensure_ascii=False), encoding="utf8"),
            stop=Event())
    finally:
        adapter.credentials.clear()
        adapter.close()
        output_state.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf8")
    (folder / "document-private.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({"new_main_ocr_submissions": 0, "fresh_crop_pass": args.fresh_crop_pass,
        "crop_missing": batch["crop_missing"], "standalone_chart_crops": batch.get("standalone_chart_crops", 0),
        "standalone_figure_crops": batch.get("standalone_figure_crops", 0),
        "charts": [{"page_no": page["page_no"], "block_id": b["block_id"], "crop": b.get("crop"),
                    "crop_provider_pass": b.get("crop_provider_pass")}
                   for page in state["page_results"] for b in page["blocks"] if b.get("provider_label") == "chart"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

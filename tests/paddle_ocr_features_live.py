"""Explicit same-page OCR feature comparison; all source text stays private."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--pages", type=int, nargs="+", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--config-id", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = (root / args.out).resolve()
    if not folder.is_relative_to((root / ".backup").resolve()):
        parser.error("Private experiment outputs must remain below .backup.")
    folder.mkdir(parents=True, exist_ok=True)
    source = Path(args.pdf).resolve(strict=True)
    baseline = json.loads((root / args.baseline).read_text(encoding="utf8"))
    with source.open("rb") as stream:
        original_sha = hashlib.file_digest(stream, "sha256").hexdigest()
    baseline_run = json.loads((root / args.baseline).parent.joinpath("run.json").read_text(encoding="utf8"))
    if original_sha not in {d["sha256"] for d in baseline_run["documents"]}:
        parser.error("Baseline source differs from supplied PDF.")
    pages = sorted(set(args.pages))
    sample = folder / "problem-pages.pdf"
    if not sample.exists():
        import pypdfium2 as pdfium
        with pdfium.PdfDocument(source) as pdf, pdfium.PdfDocument.new() as selected:
            if any(not 1 <= p <= len(pdf) for p in pages):
                parser.error("Invalid source page.")
            selected.import_pages(pdf, [p-1 for p in pages])
            selected.save(sample)
        (folder / "scope.json").write_text(json.dumps(
            {"source_sha256": original_sha, "original_pages": pages,
             "sample_sha256": hashlib.sha256(sample.read_bytes()).hexdigest()}, indent=2), encoding="utf8")
    scope = json.loads((folder / "scope.json").read_text(encoding="utf8"))
    if scope != {"source_sha256": original_sha, "original_pages": pages,
                 "sample_sha256": hashlib.sha256(sample.read_bytes()).hexdigest()}:
        parser.error("Existing sample scope changed.")
    upload = folder / "upload"
    completed = upload / "document-1-private.json"
    if not completed.exists():
        subprocess.run([sys.executable, str(root / "tests/document_attachments_files_live.py"),
            "--live", "--protocol", "paddleocr_vl", "--config-id", args.config_id,
            "--files", str(sample), "--output", str(upload.relative_to(root)),
            "--capture-provider-results"], cwd=root, check=True)
    current = json.loads(completed.read_text(encoding="utf8"))
    sys.path.insert(0, str(root / "server/backend"))
    from app.materials.question_extraction import LABEL, blocks_and_anchors
    def metrics(page):
        blocks = page["blocks"]
        text = "\n".join(b.get("content", "") for b in blocks)
        return {"labels": len(LABEL.findall(text)),
            "unique_labels": len(blocks_and_anchors({"page_results": [page]})[1]), "chars": len(text),
            "figure_text_chars": sum(len(b.get("content", "")) for b in blocks if b["type"] == "figure"),
            "figures_without_text": sum(b["type"] == "figure" and not b.get("content") for b in blocks),
            "seal_blocks": sum(b.get("source_kind") == "seal" for b in blocks),
            "preprocessing": page.get("preprocessing"),
            "review_reasons": page.get("review_reasons", [])}
    by_page = {p["page_no"]: p for p in baseline["page_results"]}
    comparison = [{"original_page": original, "before": metrics(by_page[original]),
                   "after": metrics(page)}
                  for original, page in zip(pages, current["page_results"], strict=True)]
    summary = {"original_pages": pages, "ocr_options": current.get("ocr_options"),
        "before_labels": sum(row["before"]["labels"] for row in comparison),
        "after_labels": sum(row["after"]["labels"] for row in comparison),
        "before_unique_labels": sum(row["before"]["unique_labels"] for row in comparison),
        "after_unique_labels": sum(row["after"]["unique_labels"] for row in comparison),
        "requested_features_confirmed": all(all(row["after"].get("preprocessing", {}).get(
            "reported", {}).get(name) is True for name in (
                "use_doc_orientation_classify", "use_doc_unwarping", "use_layout_detection",
                "use_chart_recognition", "use_seal_recognition", "use_ocr_for_image_block"))
            for row in comparison),
        "figure_text_chars": sum(row["after"]["figure_text_chars"] for row in comparison),
        "pages_with_unmapped_coordinates": sum(
            row["after"].get("preprocessing", {}).get("coordinate_space") == "processed" for row in comparison)}
    (folder / "comparison-private.json").write_text(json.dumps(
        {"summary": summary, "pages": comparison}, ensure_ascii=False, indent=2), encoding="utf8")
    upload_run_path = upload / "run.json"
    upload_run = json.loads(upload_run_path.read_text(encoding="utf8"))
    if not upload_run.get("schema_cleaned"):
        schema = upload_run["schema"]
        if not re.fullmatch(r"document_files_[a-f0-9]{32}", schema):
            raise ValueError("unexpected_probe_schema")
        if not all(d.get("metrics", {}).get("status") == "parsed" for d in upload_run["documents"]):
            raise ValueError("cannot_clean_incomplete_probe")
        from dotenv import load_dotenv
        from sqlalchemy import create_engine, text
        load_dotenv(root / "server/.env")
        from app.logging_audit.db import get_database_url
        engine = create_engine(get_database_url())
        try:
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            engine.dispose()
        upload_run["schema_cleaned"] = True
        upload_run_path.write_text(json.dumps(upload_run, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

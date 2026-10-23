"""Opt-in same-page provider comparison; credential is read without terminal echo."""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path


def metrics(page, label_pattern):
    blocks = page.get("blocks", [])
    # Provider understanding is auxiliary, never counted as literal OCR.
    text = "\n".join(b.get("content", "") for b in blocks
        if b.get("source_kind") not in ("image_understanding", "seal") and not b.get("chart_conversion"))
    image_text = "\n".join(b.get("content", "") for b in blocks if b["type"] == "figure")
    lines = [line.strip() for line in image_text.splitlines() if line.strip()]
    return {"labels": list(dict.fromkeys(m.group().strip() for m in label_pattern.finditer(text))),
        "ocr_chars": len(text),
        "image_ocr_chars": sum(len(b.get("content", "")) for b in blocks if b["type"] == "figure"),
        "image_understanding_chars": sum(len(b.get("image_understanding", "")) for b in blocks),
        "image_nonempty_lines": len(lines), "image_unique_lines": len(set(lines)),
        "max_repeated_image_line": max(Counter(lines).values(), default=0),
        "figures_without_text": sum(b["type"] == "figure" and not b.get("content") for b in blocks),
        "tables": sum(b["type"] == "table" for b in blocks),
        "formulas": sum(b["type"] == "formula" for b in blocks)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--pages", type=int, nargs="+", required=True)
    parser.add_argument("--paddle-run", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = (root / args.out).resolve()
    if not folder.is_relative_to((root / ".backup").resolve()):
        parser.error("Private output must remain below .backup.")
    folder.mkdir(parents=True, exist_ok=True)
    paddle_folder = (root / args.paddle_run).resolve()
    scope = json.loads((paddle_folder / "scope.json").read_text(encoding="utf8"))
    source = Path(args.pdf).resolve(strict=True)
    with source.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    pages = sorted(set(args.pages))
    if sha != scope["source_sha256"] or not set(pages) <= set(scope["original_pages"]):
        parser.error("Paddle baseline does not match requested source pages.")
    progress_path = folder / "run.json"
    progress = json.loads(progress_path.read_text(encoding="utf8")) if progress_path.exists() else {}
    if progress and (progress["sha256"] != sha or progress["original_pages"] != pages):
        parser.error("Existing experiment scope changed.")
    if progress.get("failure"):
        parser.error("A prior failure requires diagnosis; do not silently repeat paid submissions.")
    sys.path.insert(0, str(root / "server/backend"))
    os.environ.update(CONTENT_TRACE_ENABLED="0", OTEL_ENABLED="0", PYDANTIC_AI_NO_BANNER="1")
    from app.logging_audit.sdk_logging import install_sdk_logging_safety
    install_sdk_logging_safety()
    from app.materials.document_pipeline import normalize_result
    from app.materials.question_extraction import LABEL
    from app.slice1.adapters.somark import SomarkAdapter
    from app.slice1.models import SOMARK_ENDPOINT
    def save():
        progress_path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf8")
    if not progress:
        import pypdfium2 as pdfium
        sample = folder / "problem-pages.pdf"
        with pdfium.PdfDocument(source) as pdf, pdfium.PdfDocument.new() as selected:
            selected.import_pages(pdf, [page-1 for page in pages])
            selected.save(sample)
        progress = {"sha256": sha, "original_pages": pages, "status": "prepared",
            "sample_sha256": hashlib.sha256(sample.read_bytes()).hexdigest(),
            "features": {"enable_title_level_recognition": True, "enable_image_ocr": True,
                         "enable_image_understanding": True, "keep_header_footer": True}}
        save()
    if progress["status"] != "completed":
        credential = getpass.getpass("SoMark API key (hidden): ")
        if not credential.strip():
            raise SystemExit("Missing credential.")
        class PrivateAdapter(SomarkAdapter):
            def _post(self, path, **kwargs):
                result = super()._post(path, **kwargs)
                if path == "/parse/async_check" and result.get("status") == "SUCCESS":
                    (folder / "provider-result-private.json").write_text(
                        json.dumps(result, ensure_ascii=False), encoding="utf8")
                return result
        adapter = PrivateAdapter(endpoint=SOMARK_ENDPOINT, credentials={"api_key": credential.strip()},
                                 max_response_bytes=32_000_000)
        credential = ""
        started = time.monotonic()
        try:
            adapter._post("/usage")
            print(json.dumps({"credential_probe": "accepted", "pages": len(pages)}), flush=True)
            if progress.get("status") == "submitting" and not progress.get("job_id"):
                raise RuntimeError("ambiguous_submission")
            if not progress.get("job_id"):
                progress["status"] = "submitting"
                save()
                payload = (folder / "problem-pages.pdf").read_bytes()
                if hashlib.sha256(payload).hexdigest() != progress["sample_sha256"]:
                    raise RuntimeError("sample_changed")
                progress["job_id"] = adapter.submit_document(payload, "pdf")
                progress["status"] = "processing"
                save()
            last_notice = 0.0
            while time.monotonic() - started < 900:
                output = adapter.check_document(progress["job_id"])
                status = output.get("status")
                if status == "SUCCESS":
                    state = normalize_result(output, expected_pages=len(pages), image_understanding_enabled=True)
                    (folder / "document-private.json").write_text(
                        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf8")
                    progress.update(status="completed", seconds=round(time.monotonic()-started, 2))
                    save()
                    break
                if status not in ("QUEUING", "PROCESSING"):
                    raise RuntimeError("provider_parse_failed")
                if time.monotonic() - last_notice >= 15:
                    print(json.dumps({"status": status, "elapsed_seconds": round(time.monotonic()-started)}), flush=True)
                    last_notice = time.monotonic()
                time.sleep(4)
            else:
                raise RuntimeError("probe_deadline_exceeded")
        except Exception as exc:  # noqa: BLE001 - never print provider bodies or credentials
            progress["failure"] = {"type": type(exc).__name__, "code": getattr(exc, "code", "PROBE_FAILED")}
            save()
            print(json.dumps({"status": "failed", **progress["failure"]}), flush=True)
            return 1
        finally:
            adapter.credentials.clear()
            adapter.close()
    state = json.loads((folder / "document-private.json").read_text(encoding="utf8"))
    paddle = json.loads((paddle_folder / "upload/document-1-private.json").read_text(encoding="utf8"))
    paddle_pages = dict(zip(scope["original_pages"], paddle["page_results"], strict=True))
    comparison = [{"original_page": original, "paddle": metrics(paddle_pages[original], LABEL),
                   "somark": metrics(current, LABEL)}
                  for original, current in zip(pages, state["page_results"], strict=True)]
    result = {"summary": {"pages": len(pages), "seconds": progress["seconds"],
                         "provider_pipeline": "production_adapter_and_normalization"},
              "comparison": comparison}
    (folder / "comparison-private.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

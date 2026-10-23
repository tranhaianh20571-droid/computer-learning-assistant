"""One pinned image-preserving pass for charts missing provider crops."""
from __future__ import annotations

import hashlib
import io
import time

from ..slice1.errors import AdapterError
from .document_pipeline import normalize_result
from .figure_crops import persist_crops
from .upload import PDFIUM_LOCK


def same_region(left, right):
    if not left or not right or len(left) != 4 or len(right) != 4:
        return False
    if max(abs(a - b) for a, b in zip(left, right, strict=True)) > .005:
        return False
    intersection = max(0, min(left[2], right[2])-max(left[0], right[0])) * max(
        0, min(left[3], right[3])-max(left[1], right[1]))
    union = (left[2]-left[0])*(left[3]-left[1]) + (right[2]-right[0])*(right[3]-right[1]) - intersection
    return union > 0 and intersection / union >= .98


def complete_chart_crops(normalized, state, batch, *, source, storage, material_id,
                         adapter, guard, checkpoint, stop):
    if batch.get("crop_status") == "completed" and any(
            block.get("crop_provider_pass") == "chart_image_original" and block.get("crop")
            for page in normalized["page_results"] for block in page["blocks"]):
        return True
    needed = [page for page in normalized["page_results"]
              if any(block.get("provider_label") == "chart" and not block.get("crop")
                     for block in page["blocks"])]
    if not needed:
        return True
    if stop.is_set():
        return False
    pages = [page["page_no"] for page in needed]
    if batch.get("crop_pages", pages) != pages:
        raise AdapterError("response_invalid")
    batch["crop_pages"] = pages
    previous = adapter.document_options
    adapter.document_options = state["ocr_crop_options"]
    try:
        if batch.get("crop_job_id") == "submitting":
            raise AdapterError("response_invalid")
        if not batch.get("crop_job_id"):
            import pypdfium2 as pdfium
            guard()
            with PDFIUM_LOCK, pdfium.PdfDocument(source) as pdf, pdfium.PdfDocument.new() as selected:
                selected.import_pages(pdf, [n - 1 for n in pages])
                output = io.BytesIO()
                selected.save(output)
            payload = output.getvalue()
            digest = hashlib.sha256(payload).hexdigest()
            batch.update(crop_status="processing", crop_job_id="submitting", crop_input_sha256=digest)
            checkpoint(state)
            guard()
            batch["crop_job_id"] = adapter.submit_document(payload, "pdf")
            checkpoint(state)
        started = time.monotonic()
        while not stop.is_set():
            guard()
            if time.monotonic() - started >= 900:
                raise AdapterError("timeout")
            result = adapter.check_document(batch["crop_job_id"])
            status = result.get("status")
            if status == "SUCCESS":
                supplemental = normalize_result(result, expected_pages=len(pages))
                for primary, extra in zip(needed, supplemental["page_results"], strict=True):
                    left, right = primary.get("preprocessing", {}), extra.get("preprocessing", {})
                    if any(left.get(k) != right.get(k) for k in ("angle", "coordinate_space")):
                        # A different frame is independent evidence, never a replacement box.
                        for other in extra["blocks"]:
                            if other.get("type") != "figure" or not other.get("provider_crop_url"):
                                continue
                            number = len(primary["blocks"])
                            primary["blocks"].append({**other, "content": "", "source_kind": "provider_crop",
                                "block_id": f'p{primary["page_no"]}/b{number}', "reading_order": number,
                                "crop_provider_pass": "chart_image_original"})
                        continue
                    for block in primary["blocks"]:
                        if block.get("provider_label") != "chart" or block.get("crop"):
                            continue
                        box = block.get("processed_bbox", block.get("source_bbox"))
                        matches = [other for other in extra["blocks"] if other["type"] == "figure"
                                   and other.get("provider_crop_url") and box
                                   and same_region(other.get("processed_bbox", other.get("source_bbox")), box)]
                        reverse = [other for other in primary["blocks"] if other["type"] == "figure"
                                   and matches and same_region(
                                       other.get("processed_bbox", other.get("source_bbox")),
                                       matches[0].get("processed_bbox", matches[0].get("source_bbox")))]
                        if len(matches) == len(reverse) == 1:
                            block.update(provider_crop_url=matches[0]["provider_crop_url"],
                                         crop_provider_pass="chart_image",
                                         crop_match_bbox=matches[0].get("processed_bbox", matches[0].get("source_bbox")))
                persist_crops(normalized, storage, material_id, adapter, guard)
                batch["crop_status"] = "completed"
                batch["crop_missing"] = sum(block.get("provider_label") == "chart" and not block.get("crop")
                    for page in needed for block in page["blocks"])
                batch["standalone_chart_crops"] = sum(block.get("crop_provider_pass") == "chart_image_original"
                    and block.get("provider_label") == "chart" and bool(block.get("crop"))
                    for page in needed for block in page["blocks"])
                batch["standalone_figure_crops"] = sum(block.get("crop_provider_pass") == "chart_image_original"
                    and bool(block.get("crop")) for page in needed for block in page["blocks"])
                checkpoint(state)
                return True
            if status not in ("QUEUING", "PROCESSING"):
                batch["crop_status"] = "failed"
                checkpoint(state)
                raise AdapterError("response_invalid")
            if stop.wait(4):
                return False
        return False
    except ValueError:
        raise AdapterError("response_invalid") from None
    finally:
        adapter.document_options = previous

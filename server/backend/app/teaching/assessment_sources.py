"""Private lesson-scoped figure catalog; URLs are always server-generated."""
import json

from sqlalchemy import select

from ..materials.models import DocumentAttachmentRow, FigureAssetRow
from ..materials.study_sources import verify_sources


def content_evidence(caption, description=""):
    """A crop locator proves availability, not what the picture contains."""
    description = str(description or caption or "").strip()
    return {"content_status": "text_available" if description else "unavailable",
            "description": description[:2000]}


def public_figure_source(reference):
    return {k: reference[k] for k in ("material_id", "page_no", "block_no", "crop_sha256",
                                     "figure_id", "asset_index", "content_hash") if k in reference}


def figure_catalog(session, owner, snapshot, lesson_id):
    result, present = [], False
    for doc in verify_sources(session, owner, snapshot):
        refs = set(snapshot.get("lessons", {}).get(lesson_id, []))
        sections = [s for s in doc["index"]["sections"] if doc["material_id"] + "/" + s["id"] in refs]
        pages = {n for s in sections for n in range(s["page_no"], s["end_page"] + 1)}
        if not pages:
            continue
        from ..materials.source_models import ImportedSourceRow
        attachment = session.get(DocumentAttachmentRow, doc["material_id"])
        imported = session.get(ImportedSourceRow, doc["material_id"]) if attachment is None else None
        attachment = attachment or imported
        state = json.loads(attachment.result_json)
        verified_figures = {}
        bank = state.get("question_bank", {})
        if bank.get("status") == "completed":
            for question in bank.get("questions", []):
                if question.get("assessment_eligible") and question.get("figures_complete"):
                    for figure in question.get("figure_roles", []):
                        if figure["role"] == "input":
                            verified_figures[figure["block_id"]] = question
        if imported and state.get("document_input", {}).get("kind") != "pdf":
            for asset in state.get("assets", []):
                if state.get("unit") == "time":
                    matched = any(s.get("start_ms", 0) <= asset.get("time_ms", -1) <= s.get("end_ms", -1) for s in sections)
                else:
                    matched = asset.get("page_no", 1 if state.get("unit") == "image" else None) in pages
                if matched:
                    present = True
                    result.append({"ref": f'{doc["material_id"]}/asset/{asset["index"]}',
                        "material_id": doc["material_id"], "asset_index": asset["index"],
                        "caption": str(asset.get("caption", ""))[:300], "unit": state.get("unit"),
                        **content_evidence(asset.get("caption", ""), asset.get("image_understanding", "")),
                        "start_ms": asset.get("time_ms"), "content_hash": doc["content_hash"]})
            continue
        for page in state.get("page_results", []):
            if page["page_no"] not in pages:
                continue
            for number, block in enumerate(page.get("blocks", [])):
                if block.get("type") != "figure":
                    continue
                present = True
                if not block.get("source_bbox") and not block.get("crop"):
                    continue
                result.append({"ref": f'{doc["material_id"]}/p{page["page_no"]}/b{number}',
                    "material_id": doc["material_id"], "page_no": page["page_no"],
                    "block_no": number, "caption": str(block.get("content", ""))[:300],
                    **content_evidence(block.get("content", ""), block.get("image_understanding", "")),
                    "content_hash": doc["content_hash"]})
                if block.get("crop"):
                    result[-1]["crop_sha256"] = block["crop"]["sha256"]
                verified = verified_figures.get(block.get("block_id", f'p{page["page_no"]}/b{number}'))
                if verified and (block.get("source_bbox") or
                        verified.get("figure_crops", {}).get(block.get("block_id")) ==
                        block.get("crop", {}).get("sha256")):
                    result[-1].update(content_status="visual_reviewed",
                        description=verified.get("visual_description") or verified["review"]["reason"],
                        question_id=doc["material_id"] + "/" + verified["id"],
                        figure_role="input")
        for figure in session.scalars(select(FigureAssetRow).where(
                FigureAssetRow.material_id == doc["material_id"], FigureAssetRow.source_page_no.in_(pages))):
            present = True
            result.append({"ref": figure.figure_id, "material_id": doc["material_id"],
                "page_no": figure.source_page_no, "figure_id": figure.figure_id,
                "caption": figure.caption_text or "", "content_hash": doc["content_hash"]})
            result[-1].update(content_evidence(figure.caption_text))
    return {"status": "available" if result else "unavailable" if present else "absent",
            "figures": result}

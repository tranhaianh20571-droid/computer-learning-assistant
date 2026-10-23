"""Evidence-bound question inventory and resumable semantic extraction."""
from __future__ import annotations

import hashlib
import io
import json
import re
from collections import Counter
from contextlib import closing
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai.exceptions import ModelRetry

VERSION = 4
PROMPTS = ("material_questions_extract_v1", "material_questions_review_v1")
LABEL = re.compile(
    r"(?im)(?:[【\[]\s*(?:例(?:题)?\s*\d+(?:\.\d+)*|example\s+\d+(?:\.\d+)*)\s*[】\]]|"
    r"^[ \t]*(?:例(?:题)?\s*\d+(?:\.\d+)*|example\s+\d+(?:\.\d+)*)(?!\d|[ \t]*[和与]))")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def label_key(value):
    return re.sub(r"[\s【】\[\].、:：．]", "", value).casefold()


class Evidence(BaseModel):
    block_id: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=16000)


class Subquestion(BaseModel):
    label: str = Field(max_length=100)
    evidence: list[Evidence] = Field(min_length=1, max_length=40)


class FigureRole(BaseModel):
    block_id: str
    role: Literal["input", "answer", "mixed", "unknown"]


class ExtractedQuestion(BaseModel):
    local_id: str = Field(min_length=1, max_length=80)
    anchor_ids: list[str] = Field(default_factory=list, max_length=100)
    label: str = Field(default="", max_length=200)
    kind: Literal["example", "exercise", "explain", "worked_diagram"]
    stem: list[Evidence] = Field(default_factory=list, max_length=60)
    conditions: list[Evidence] = Field(default_factory=list, max_length=60)
    options: list[Evidence] = Field(default_factory=list, max_length=60)
    subquestions: list[Subquestion] = Field(default_factory=list, max_length=80)
    answer: list[Evidence] = Field(default_factory=list, max_length=80)
    figure_roles: list[FigureRole] = Field(default_factory=list, max_length=40)
    boundary: Literal["complete", "missing_conditions", "ambiguous", "worked_only"]
    needs_figure: bool
    feynman_suitable: bool
    explanation: str = Field(max_length=1200)
    knowledge_tags: list[str] = Field(default_factory=list, max_length=20)


class Exclusion(BaseModel):
    anchor_id: str
    reason: str = Field(min_length=1, max_length=1000)


class Extraction(BaseModel):
    questions: list[ExtractedQuestion] = Field(max_length=100)
    exclusions: list[Exclusion] = Field(default_factory=list, max_length=100)
    visual_review_pages: list[int] = Field(default_factory=list, max_length=20)


class Verdict(BaseModel):
    local_id: str
    complete: bool
    answer_isolated: bool
    single_question: bool = Field(description="同一原题组，可以有多个小问；混入其他原题才为false。")
    figures_complete: bool
    feynman_suitable: bool
    reason: str = Field(min_length=1, max_length=1200)
    independent_subquestions: bool = False
    visual_description: str = Field(default="", max_length=4000,
        description="实际看图后记录作答所需的可见条件、标注、连接和极性；不推导答案，不冒充逐字OCR。")


class ExclusionVerdict(BaseModel):
    anchor_id: str
    accepted: bool
    reason: str = Field(min_length=1, max_length=1000)


class Review(BaseModel):
    verdicts: list[Verdict] = Field(max_length=100)
    exclusions: list[ExclusionVerdict] = Field(default_factory=list, max_length=100)


def blocks_and_anchors(state):
    from .study_sources import block_text
    blocks, anchors, cursor = [], [], 0
    for page in sorted(state.get("page_results", []), key=lambda p: p["page_no"]):
        emitted = False
        for ordinal, raw in enumerate(page.get("blocks", [])):
            text = block_text(raw)
            ident = raw.get("block_id", f"p{page['page_no']}/b{ordinal}")
            if text:
                if emitted:
                    cursor += 1
                offset = cursor
                cursor += len(text)
                emitted = True
            else:
                offset = cursor
            item = {"block_id": ident, "page_no": page["page_no"], "type": raw.get("type", "text"),
                "text": text, "source_offset": offset,
                "source_kind": raw.get("source_kind", "ocr"),
                "bbox": raw.get("source_bbox", raw.get("processed_bbox", raw.get("bbox"))),
                "coordinate_space": raw.get("coordinate_space", "original"),
                "reading_order": raw.get("reading_order", ordinal)}
            if raw.get("crop"):
                item["crop"] = raw["crop"]
            if raw.get("format") == "html":
                item["html"] = raw.get("content", "")
            # Retain footer data for classification, without adding it to frozen OCR offsets.
            if raw.get("type") in ("header", "footer"):
                item["text"] = raw.get("content", "")
                item["source_kind"] = "peripheral"
            blocks.append(item)
            for match in LABEL.finditer(item["text"]):
                duplicate = None
                if item["bbox"]:
                    for previous in anchors:
                        prior = next(b for b in blocks if b["block_id"] == previous["block_id"])
                        box, other = item["bbox"], prior.get("bbox")
                        if (previous["page_no"] != page["page_no"] or not other
                                or "image_ocr" not in (prior["source_kind"], item["source_kind"])
                                or prior["coordinate_space"] != item["coordinate_space"]
                                or label_key(previous["label"]) != label_key(match.group())):
                            continue
                        overlap = max(0, min(box[2], other[2])-max(box[0], other[0])) * max(
                            0, min(box[3], other[3])-max(box[1], other[1]))
                        smaller = min((box[2]-box[0])*(box[3]-box[1]),
                                      (other[2]-other[0])*(other[3]-other[1]))
                        if smaller > 0 and overlap >= .8 * smaller:
                            duplicate = previous
                            break
                if duplicate is not None:
                    duplicate.setdefault("alternate_locations", []).append({
                        "block_id": ident, "local_start": match.start(), "source_kind": item["source_kind"]})
                    continue
                anchors.append({"id": "anchor_" + fingerprint([ident, match.start()])[:20],
                    "page_no": page["page_no"], "block_id": ident, "label": match.group().strip(),
                    "local_start": match.start(), "source_kind": item["source_kind"]})
        cursor += 1
    # Native PDF is independent evidence. Merge only identical labels on the same page.
    known = {(a["page_no"], label_key(a["label"])) for a in anchors}
    for raw in state.get("native_pdf_blocks", []):
        blocks.append(raw)
        for match in LABEL.finditer(raw["text"]):
            label = match.group().strip()
            key = raw["page_no"], label_key(label)
            if key not in known:
                anchors.append({"id": "anchor_" + fingerprint([raw["block_id"], match.start()])[:20],
                    "page_no": raw["page_no"], "block_id": raw["block_id"], "label": label,
                    "local_start": match.start(), "source_kind": "native_pdf"})
                known.add(key)
    return blocks, anchors


def add_native_pdf(state, source, guard):
    """Keep native words separate; never splice them into OCR or its frozen index."""
    if "native_pdf_blocks" in state:
        return
    import pypdfium2 as pdfium

    from .upload import PDFIUM_LOCK
    blocks = []
    with PDFIUM_LOCK, pdfium.PdfDocument(source) as pdf:
        for number in range(len(pdf)):
            guard()
            with closing(pdf[number]) as page, closing(page.get_textpage()) as textpage:
                text = textpage.get_text_range()
                # Store only label-bearing lines; native watermarks are not extra study content.
                for match in LABEL.finditer(text):
                    start = text.rfind("\n", 0, match.start()) + 1
                    end = text.find("\n", match.end())
                    end = len(text) if end < 0 else end
                    width, height = page.get_size()
                    try:
                        left, bottom, right, top = textpage.get_charbox(match.start())
                        box = [left / width, 1 - top / height, right / width, 1 - bottom / height]
                    except (ValueError, IndexError):
                        box = None
                    blocks.append({"block_id": f"native/p{number+1}/{match.start()}",
                        "page_no": number+1, "type": "text", "text": text[start:end].strip(),
                        "bbox": box, "source_offset": None, "reading_order": match.start(),
                        "source_kind": "native_pdf"})
    state["native_pdf_blocks"] = blocks


def windows(blocks, anchors, *, max_pages=4, max_chars=20000):
    page_numbers = sorted({b["page_no"] for b in blocks})
    result, primary, size = [], [], 0
    def append():
        if not primary:
            return
        selected = [b for b in blocks if primary[0]-1 <= b["page_no"] <= primary[-1]+1]
        result.append({"primary_pages": list(primary), "source_blocks": selected,
            "anchors": [a for a in anchors if a["page_no"] in primary]})
    for number in page_numbers:
        page_size = sum(len(b["text"]) + len(b.get("html", "")) for b in blocks if b["page_no"] == number)
        if primary and (len(primary) >= max_pages or size + page_size > max_chars):
            append()
            primary, size = [], 0
        primary.append(number)
        size += page_size
    append()
    return result


def question_evidence(question):
    return [*question.stem, *question.conditions, *question.options,
            *(e for part in question.subquestions for e in part.evidence)]


def validate_extraction(output, window):
    if not set(output.visual_review_pages) <= {b["page_no"] for b in window["source_blocks"]}:
        raise ModelRetry("视觉复核页必须属于本次资料窗口。")
    blocks = {b["block_id"]: b for b in window["source_blocks"]}
    anchors = {a["id"]: a for a in window["anchors"]}
    used = []
    if len({q.local_id for q in output.questions}) != len(output.questions):
        raise ModelRetry("local_id不得重复。")
    for question in output.questions:
        for evidence in [*question_evidence(question), *question.answer]:
            block = blocks.get(evidence.block_id)
            if not block or evidence.text not in block["text"]:
                raise ModelRetry("Evidence引用不匹配，block_id=" + evidence.block_id +
                    "。须逐字引用该block.text的连续子串，不改空格、公式，不引用html字段。")
        used.extend(question.anchor_ids)
        cited = [blocks[e.block_id] for e in question_evidence(question)]
        if not question.anchor_ids and (not cited or cited[0]["page_no"] not in window["primary_pages"]):
            raise ModelRetry("无编号题必须从primary_pages真实正文开始。")
        if any(a not in anchors for a in question.anchor_ids):
            raise ModelRetry("anchor_ids只能引用当前窗口定位项。")
        if any(role.block_id not in blocks or blocks[role.block_id]["type"] != "figure"
               for role in question.figure_roles):
            raise ModelRetry("figure_roles只能引用真实figure块。")
        if len({role.block_id for role in question.figure_roles}) != len(question.figure_roles):
            raise ModelRetry("同一figure块只能有一个角色；题设与答案无法分离时须标记mixed。")
    used.extend(e.anchor_id for e in output.exclusions)
    if Counter(used) != Counter(anchors.keys()):
        raise ModelRetry("每个anchor须恰好归属一道题或一个exclusions记录，不能遗漏或重复。")
    return output


def validate_review(output, candidate):
    if Counter(v.local_id for v in output.verdicts) != Counter(q.local_id for q in candidate.questions):
        raise ModelRetry("verdicts须完整且逐一对应候选local_id，不重复。")
    if Counter(v.anchor_id for v in output.exclusions) != Counter(e.anchor_id for e in candidate.exclusions):
        raise ModelRetry("须独立复核全部排除记录。")
    return output


def compile_questions(extraction, review, window, sections):
    blocks = {b["block_id"]: b for b in window["source_blocks"]}
    anchors = {a["id"]: a for a in window["anchors"]}
    verdicts = {v.local_id: v for v in review.verdicts}
    result = []
    for question in extraction.questions:
        verdict = verdicts[question.local_id]
        evidence = question_evidence(question)
        locations = [blocks[e.block_id] for e in evidence]
        anchor = anchors[question.anchor_ids[0]] if question.anchor_ids else None
        page_no = anchor["page_no"] if anchor else locations[0]["page_no"]
        offset = next((b["source_offset"] + b["text"].find(e.text)
            for e, b in zip(evidence, locations, strict=True) if b["source_offset"] is not None), None)
        if offset is None:
            offset = next((b["source_offset"] for b in window["source_blocks"]
                if b["page_no"] == page_no and b["source_offset"] is not None), 0)
        section = next((s for s in reversed(sections)
            if s["offset"] <= offset < s["end_offset"] and not s.get("reference_only")), None)
        prompt = "\n".join(e.text for e in evidence).strip()
        figures_seen = all(blocks[f.block_id]["page_no"] in window.get("visual_pages", [])
                           for f in question.figure_roles if f.role != "answer")
        unsafe_figures = any(f.role in ("mixed", "unknown") for f in question.figure_roles)
        unsafe_figures |= any(blocks[f.block_id].get("coordinate_space") == "processed"
                              and not (blocks[f.block_id].get("crop")
                                       and f.block_id in window.get("visual_figures", []))
                              for f in question.figure_roles if f.role != "answer")
        input_figures = any(f.role == "input" for f in question.figure_roles)
        question_pages = {page_no, *(b["page_no"] for b in locations),
                          *(blocks[f.block_id]["page_no"] for f in question.figure_roles)}
        unseen_unclear = (set(extraction.visual_review_pages) & question_pages) - set(window.get("visual_pages", []))
        complete = bool(question.boundary == "complete" and verdict.complete and verdict.single_question
                        and not unseen_unclear)
        answer_safe = verdict.answer_isolated
        visual_ok = verdict.figures_complete and (
            not question.needs_figure and not input_figures
            or question.needs_figure and input_figures and figures_seen and not unsafe_figures)
        assessment = bool(prompt and section and complete and answer_safe and visual_ok
                          and len(prompt) <= 1600
                          and sum(f.role == "input" for f in question.figure_roles) <= 1)
        status = ("confirmed" if assessment else "teaching_only" if question.kind == "worked_diagram"
                  else "needs_review")
        identity = question.anchor_ids or [[e.block_id, e.text] for e in evidence]
        result.append({"id": "semantic_" + fingerprint(identity)[:20],
            "anchor_ids": question.anchor_ids, "label": question.label,
            "prompt": prompt, "start": offset, "end": offset + len(prompt),
            "page_no": page_no, "end_page": max([page_no, *(b["page_no"] for b in locations)]),
            "section_id": section["id"] if section else "", "kind": question.kind,
            "requires_context": not assessment, "status": status,
            "answer_text": "\n".join(e.text for e in question.answer),
            "boundary": question.boundary, "answer_isolated": answer_safe,
            "needs_figure": question.needs_figure, "figures_complete": visual_ok,
            "figure_roles": [f.model_dump() for f in question.figure_roles],
            "figure_crops": {f.block_id: blocks[f.block_id]["crop"]["sha256"]
                            for f in question.figure_roles if f.role == "input"
                            and blocks[f.block_id].get("crop") and f.block_id in window.get("visual_figures", [])},
            "visual_description": verdict.visual_description if figures_seen and visual_ok
                                  and question.needs_figure else "",
            "teaching_eligible": bool(prompt and verdict.single_question),
            "assessment_eligible": assessment,
            "feynman_suitable": bool(assessment and not question.needs_figure
                                    and question.feynman_suitable and verdict.feynman_suitable),
            "evidence": [e.model_dump() for e in evidence],
            "subquestions": [s.model_dump() for s in question.subquestions],
            "options": [e.model_dump() for e in question.options],
            "knowledge_tags": question.knowledge_tags,
            "review": verdict.model_dump(), "extractor_version": VERSION,
            "transcription_verified": False})
    expanded = []
    for question, compiled in zip(extraction.questions, result, strict=True):
        expanded.append(compiled)
        if len(compiled["prompt"]) <= 1600 or not verdicts[question.local_id].independent_subquestions:
            continue
        common = [*question.stem, *question.conditions, *question.options]
        parent = compiled["id"]
        for part in question.subquestions:
            fragment = "\n".join(e.text for e in [*common, *part.evidence])
            if not 8 <= len(fragment) <= 1600:
                continue
            eligibility = bool(compiled["boundary"] == "complete" and compiled["answer_isolated"]
                and compiled["figures_complete"] and verdicts[question.local_id].complete
                and verdicts[question.local_id].single_question and compiled["section_id"]
                and sum(f.role == "input" for f in question.figure_roles) <= 1)
            expanded.append({**compiled, "id": parent + "_" + fingerprint(part.model_dump())[:10],
                "parent_id": parent, "anchor_ids": [], "prompt": fragment,
                "end": compiled["start"] + len(fragment),
                "evidence": [e.model_dump() for e in [*common, *part.evidence]],
                "subquestions": [part.model_dump()], "assessment_eligible": eligibility,
                "requires_context": not eligibility, "feynman_suitable": False,
                "status": "confirmed" if eligibility else "needs_review"})
    return expanded


def render_pages(source, page_numbers, guard):
    import pypdfium2 as pdfium

    from .upload import PDFIUM_LOCK
    result = []
    with PDFIUM_LOCK, pdfium.PdfDocument(source) as pdf:
        for number in page_numbers:
            guard()
            with closing(pdf[number-1]) as page:
                scale = min(2, 1800 / max(page.get_size()))
                with closing(page.render(scale=scale)) as bitmap:
                    image = bitmap.to_pil()
                    output = io.BytesIO()
                    image.convert("RGB").save(output, "JPEG", quality=88)
                    result.append((number, output.getvalue()))
    return result


def summarize_bank(bank):
    completed = [w for w in bank["windows"].values() if w["status"] == "completed"]
    bank["questions"] = [q for w in completed for q in w.get("questions", [])]
    inventory = {item["anchor_id"]: item for w in bank["windows"].values()
                 for item in w.get("inventory", [])}
    bank["inventory"] = [inventory.get(a["id"], {"anchor_id": a["id"], "status": "pending"})
                         for a in bank.get("anchors", [])]
    bank["counts"] = {"anchors": len(bank.get("anchors", [])), "questions": len(bank["questions"]),
        "statuses": dict(Counter(i["status"] for i in bank["inventory"])),
        "completed_windows": len(completed),
        "assessment_eligible": sum(q["assessment_eligible"] for q in bank["questions"])}


def extract_questions(state, *, invoke, prompts, checkpoint, guard, source=None, visual=False,
                      max_pages=4, window_limit=None, crop_loader=None):
    """Both roles share the caller's pinned model, durable budget and task guard."""
    source_hash = fingerprint({k: state.get(k) for k in ("page_results", "native_pdf_blocks", "unit")})
    signature = fingerprint([VERSION, source_hash, prompts, visual, max_pages])
    bank = state.setdefault("question_bank", {"version": VERSION, "signature": signature,
        "source_hash": source_hash, "prompt_hashes": [fingerprint(p) for p in prompts],
        "windows": {}, "questions": [], "inventory": [], "status": "processing"})
    if bank["signature"] != signature:
        raise ValueError("question_extraction_snapshot_changed")
    blocks, anchors = blocks_and_anchors(state)
    bank["anchors"] = anchors
    bank["status"] = "processing"
    summarize_bank(bank)
    for number, window in enumerate(windows(blocks, anchors, max_pages=max_pages)):
        if window_limit is not None and number >= window_limit:
            return bank
        guard()
        key = fingerprint(window)
        saved = bank["windows"].get(key)
        if saved and saved["status"] == "completed":
            continue
        current = bank["windows"].setdefault(key, {"status": "pending", "attempts": 0,
            "primary_pages": window["primary_pages"]})
        # Debit attempts before calls, so interruption never silently resets costs.
        current["attempts"] += 1
        current["status"] = "processing"
        current["inventory"] = [{"anchor_id": a["id"], "status": "processing"} for a in window["anchors"]]
        checkpoint(state)
        window["visual_pages"] = []
        window["visual_figures"] = []
        images = []
        try:
            base = current.get("text_extraction", current.get("extraction"))
            if base is not None:
                text_extraction = validate_extraction(Extraction.model_validate(base), window)
            else:
                text_extraction = invoke(Extraction, prompts[0], window, [],
                    lambda output, window=window: validate_extraction(output, window), "material_questions_extract")
                guard()
                current["text_extraction"] = text_extraction.model_dump()
                checkpoint(state)
            needs_visual = (any(q.needs_figure or q.kind == "worked_diagram"
                                for q in text_extraction.questions)
                            or bool(text_extraction.visual_review_pages)
                            or any(a["source_kind"] == "native_pdf" for a in window["anchors"]))
            image_pages = {b["page_no"] for b in window["source_blocks"] if b["type"] == "figure"}
            image_pages.update(a["page_no"] for a in window["anchors"] if a["source_kind"] == "native_pdf")
            image_pages.update(text_extraction.visual_review_pages)
            if needs_visual and not image_pages:
                image_pages.update(window["primary_pages"])
            if needs_visual and source and visual and image_pages:
                images = render_pages(source, sorted(image_pages), guard)
                window["visual_pages"] = [n for n, _ in images]
                if crop_loader:
                    crops = crop_loader(window, guard)
                    window["visual_figures"] = [ref for ref, _ in crops]
                    images.extend(crops)
            current["visual_required"] = needs_visual
            current["visual_pages"] = window["visual_pages"]
            current["visual_figures"] = window["visual_figures"]
            if "extraction" in current:
                extracted = Extraction.model_validate(current["extraction"])
                validate_extraction(extracted, window)
            elif images:
                extracted = invoke(Extraction, prompts[0],
                    {**window, "prior_candidate": text_extraction.model_dump()}, images,
                    lambda output, window=window: validate_extraction(output, window), "material_questions_extract")
                guard()
                current["extraction"] = extracted.model_dump()
                checkpoint(state)
            else:
                extracted = text_extraction
                current["extraction"] = extracted.model_dump()
                checkpoint(state)
            reviewed = invoke(Review, prompts[1], {**window, "candidate": extracted.model_dump()},
                images, lambda output, extracted=extracted: validate_review(output, extracted), "material_questions_review")
            guard()
            current.update(status="completed", review=reviewed.model_dump(),
                questions=compile_questions(extracted, reviewed, window, state["study_index"]["sections"]))
            rejected = {v.anchor_id: v for v in reviewed.exclusions}
            current["inventory"] = [
                {"anchor_id": a, "status": q["status"], "question_id": q["id"]}
                for q in current["questions"] for a in q["anchor_ids"]]
            current["inventory"].extend({"anchor_id": e.anchor_id,
                "status": "excluded" if rejected[e.anchor_id].accepted else "needs_review",
                "reason": e.reason, "review": rejected[e.anchor_id].model_dump()}
                for e in extracted.exclusions)
            summarize_bank(bank)
            checkpoint(state)
        except Exception:
            current["status"] = "failed"
            current["inventory"] = [{"anchor_id": a["id"], "status": "failed"} for a in window["anchors"]]
            bank["status"] = "failed"
            summarize_bank(bank)
            checkpoint(state)
            raise
    summarize_bank(bank)
    if Counter(i["anchor_id"] for i in bank["inventory"]) != Counter(a["id"] for a in anchors):
        raise ValueError("question_inventory_incomplete")
    bank["status"] = "completed"
    checkpoint(state)
    return bank

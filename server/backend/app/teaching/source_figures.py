"""Resolve figure mentions against frozen source geometry, never model coordinates."""
import re

from ..materials.models import DocumentAttachmentRow
from ..materials.source_models import ImportedSourceRow
from ..planning.models import load_json_field

FIGURE = re.compile(r"(?:\bfig(?:ure)?\.?\s*|图\s*)(\d+|[一二三四五六七八九十]+)", re.I)


def figure_number(value):
    if value.isdecimal():
        return str(int(value))
    digits = {c: n for n, c in enumerate("零一二三四五六七八九")}
    if value == "十":
        return "10"
    if "十" in value:
        left, right = value.split("十", 1)
        return str((digits.get(left, 1) if left else 1) * 10 + digits.get(right, 0))
    return str(digits.get(value, 0))


def figure_targets(service, conv, context, docs):
    from .interactive import pdf_page_targets, source_path
    result = []
    with service.db.session() as sess:
        for doc in docs:
            refs = set(context["source_snapshot"].get("lessons", {}).get(conv.lesson_id, []))
            pages = {page for section in doc["index"]["sections"]
                if doc["material_id"] + "/" + section["id"] in refs
                for page in range(section["page_no"], section["end_page"] + 1)}
            readers = context["source_snapshot"].get("reading_lessons", [])
            first_reader = next((ident for ident in readers if any(
                ref.startswith(doc["material_id"] + "/") for ref in
                context["source_snapshot"].get("lessons", {}).get(ident, []))), conv.lesson_id)
            if pages and first_reader == conv.lesson_id and context.get("include_paper_opening", True):
                pages.update(range(1, min(pages) + 1))
            row = sess.get(DocumentAttachmentRow, doc["material_id"]) or sess.get(ImportedSourceRow, doc["material_id"])
            state = load_json_field(row.result_json, {}) if row else {}
            path = None
            for page in state.get("page_results", []):
                if page["page_no"] not in pages:
                    continue
                blocks = page.get("blocks", [])
                if path is None:
                    path = source_path(service, conv.owner_user_id, context["source_snapshot"],
                        doc["material_id"], doc["sha256"])
                targets = pdf_page_targets(path, doc["material_id"], doc["sha256"], page["page_no"], blocks)
                captions = {}
                for native in targets:
                    match = re.match(r"^(?:figure|fig\.?|图)\s*(\d+|[一二三四五六七八九十]+)\s*[:：.．|｜]",
                        native.get("text", ""), re.I)
                    if match and native.get("precision") == "text":
                        captions.setdefault(figure_number(match.group(1)), []).append(native)
                for index, block in enumerate(blocks):
                    if block.get("type") != "figure":
                        continue
                    caption = str(block.get("content", ""))
                    match = FIGURE.search(caption)
                    if not match:
                        neighbours = blocks[max(0, index-1):index] + blocks[index+1:index+3]
                        match = next((m for b in neighbours if (m := FIGURE.match(str(b.get("content", "")).strip()))), None)
                    if not match:
                        continue
                    target = next((t for t in targets if t["target_id"] == f'p{page["page_no"]}_b{index}'), None)
                    if target is None:
                        target = targets[0]
                    # Reading stays in the original page viewer, including while
                    # the learner is reviewing a different page.
                    box = block.get("source_bbox")
                    if (isinstance(box, list) and len(box) == 4
                            and all(type(v) in (float, int) and 0 <= v <= 1 for v in box)
                            and box[0] < box[2] and box[1] < box[3]):
                        target = {**target, "bbox": box, "precision": "ocr_region", "regions": []}
                    elif len(captions.get(figure_number(match.group(1)), [])) == 1:
                        target = captions[figure_number(match.group(1))][0]
                    target = {key: value for key, value in target.items()
                        if key not in ("crop_sha256", "crop_width", "crop_height", "block_no")}
                    if not target.get("bbox"):
                        target = {**target, "precision": "page"}
                    result.append({**target, "figure_no": figure_number(match.group(1))})
                for number, choices in captions.items():
                    if len(choices) == 1:
                        result.append({**choices[0], "figure_no": number})
    groups = {}
    for target in result:
        key = (target["material_id"], target["figure_no"], target["page_no"])
        groups.setdefault(key, {})[target["target_id"]] = target
    unique = []
    for choices in groups.values():
        priority = {"ocr_region": 3, "text": 2, "page": 1}
        best = max(priority.get(t["precision"], 0) for t in choices.values())
        unique.extend(t for t in choices.values() if priority.get(t["precision"], 0) == best)
    return unique[:240]


def figure_cues(text, targets, material_id):
    """A mention owns its sentence; ambiguous figure labels remain unpointed."""
    cues = []
    for sentence in re.finditer(r"[^。！？!?\n]+[。！？!?\n]?", text):
        mentions = list(FIGURE.finditer(sentence.group()))
        numbers = {figure_number(m.group(1)) for m in mentions}
        if len(numbers) != 1:
            continue
        matches = [t for t in targets if t["material_id"] == material_id and t["figure_no"] in numbers]
        unique = {(t["page_no"], t["target_id"]): t for t in matches}
        if len(unique) != 1:
            continue
        target = next(iter(unique.values()))
        cues.append({"start": sentence.start(), "end": sentence.end(), "pdf_focus": target,
                     "quote": sentence.group()})
    return cues

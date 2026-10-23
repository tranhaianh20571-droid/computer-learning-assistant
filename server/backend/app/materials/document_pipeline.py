"""Page-complete PDF batching and faithful, owner-private document outputs."""
from __future__ import annotations

import io
import json
import math
import os
import re
from html import escape
from html.parser import HTMLParser

from .upload import PDFIUM_LOCK

MAX_PDF_BYTES = 512_000_000
MAX_PDF_PAGES = 2000
PROVIDER_MAX_BYTES = 200_000_000
PROVIDER_MAX_PAGES = 300
DIRECT_MAX_PAGES = 50
SHORT_MAX_PAGES = 10
BATCH_TARGET_BYTES = 100_000_000


def local_page_limit():
    from ..cleanup.budget import current_budget
    return 10 if current_budget() or os.environ.get("LOCAL_PARSER_CHILD") == "1" else DIRECT_MAX_PAGES


def document_strategy(pages, size, *, target_bytes=None, page_limit=None):
    target_bytes = BATCH_TARGET_BYTES if target_bytes is None else target_bytes
    if pages is None:
        return "direct"
    if pages <= SHORT_MAX_PAGES and size <= target_bytes:
        return "short"
    if pages <= (local_page_limit() if page_limit is None else page_limit) and size <= target_bytes:
        return "direct"
    return "batched"


def pdf_batch_bytes(source, start, end):
    """Copy physical pages without rendering, recompressing or changing order."""
    import pypdfium2 as pdfium
    with PDFIUM_LOCK, pdfium.PdfDocument(source) as original, pdfium.PdfDocument.new() as part:
        part.import_pages(original, list(range(start - 1, end)))
        output = io.BytesIO()
        part.save(output)
        return output.getvalue()


def plan_batches(source, *, kind, pages, size, target_bytes=None, provider_bytes=None, page_limit=None):
    target_bytes = BATCH_TARGET_BYTES if target_bytes is None else target_bytes
    provider_bytes = PROVIDER_MAX_BYTES if provider_bytes is None else provider_bytes
    strategy = document_strategy(pages, size, target_bytes=target_bytes, page_limit=page_limit)
    if kind != "pdf" or strategy != "batched":
        batches = [{"start": 1, "end": pages, "size_bytes": size, "status": "pending"}]
    else:
        batches = []

        def add(start, end):
            payload = pdf_batch_bytes(source, start, end)
            length = len(payload)
            del payload
            if length > target_bytes and start < end:
                middle = (start + end) // 2
                add(start, middle)
                add(middle + 1, end)
            elif length > provider_bytes:
                raise ValueError("single_page_too_large")
            else:
                batches.append({"start": start, "end": end, "size_bytes": length, "status": "pending"})

        page_limit = local_page_limit() if page_limit is None else page_limit
        for start in range(1, pages + 1, page_limit):
            add(start, min(start + page_limit - 1, pages))
    return {"pipeline_version": 2, "pages": pages, "strategy": strategy,
            "batches": batches, "chunks": [], "page_results": [], "outline": []}


class TableText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []

    def handle_data(self, data):
        self.text.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th", "tr"):
            self.text.append("\n")


class SafeTable(HTMLParser):
    tags = {"table", "thead", "tbody", "tfoot", "tr", "td", "th", "br", "sup", "sub"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.row_columns = 0
        self.max_columns = 0

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row_columns = 0
        elif tag in ("td", "th"):
            span = dict(attrs).get("colspan", "1")
            self.row_columns += int(span) if span and span.isdigit() and len(span) <= 4 else 1
            self.max_columns = max(self.max_columns, self.row_columns)
        if tag in self.tags:
            spans = "".join(f' {name}="{value}"' for name, value in attrs
                            if tag in ("td", "th") and name in ("rowspan", "colspan")
                            and value and value.isdigit() and 1 <= int(value) <= 1000)
            self.parts.append(f"<{tag}{spans}>")

    def handle_endtag(self, tag):
        if tag in self.tags and tag != "br":
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(escape(data))


def clean_text(value):
    return "".join(c for c in value if c in "\n\t" or ord(c) >= 32).strip()


def paddle_document_result(layouts, *, requested_options=None):
    """Translate layout blocks; chart-derived data never becomes verbatim evidence."""
    pages = []
    for number, layout in enumerate(layouts):
        result = layout["prunedResult"]
        width, height = result["width"], result["height"]
        if (type(width) not in (int, float) or type(height) not in (int, float)
                or not 0 < width <= 100_000 or not 0 < height <= 100_000):
            raise ValueError("invalid_page_dimensions")
        blocks = []
        settings = result.get("model_settings", {})
        settings = settings if isinstance(settings, dict) else {}
        preprocessor = result.get("doc_preprocessor_res", {})
        preprocessor = preprocessor if isinstance(preprocessor, dict) else {}
        pre_settings = preprocessor.get("model_settings", {})
        pre_settings = pre_settings if isinstance(pre_settings, dict) else {}
        reported = {k: v for k, v in {**settings, **pre_settings}.items() if type(v) is bool}
        requested = dict(requested_options or {})
        # The service can transform pixels without returning an inverse geometry map.
        unwarped = reported.get("use_doc_unwarping", requested.get("useDocUnwarping", False))
        angle = preprocessor.get("angle")
        oriented = requested.get("useDocOrientationClassify", False) and angle not in (0, -1)
        transformed = bool(unwarped or oriented)
        rows = result["parsing_res_list"]
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("invalid_blocks")
        for raw in rows:
            label, value = raw.get("block_label"), raw.get("block_content", "")
            if not isinstance(value, str):
                raise ValueError("invalid_text")
            box = raw.get("block_bbox")
            source_box = None
            if (isinstance(box, list) and len(box) == 4
                    and all(type(n) in (int, float) and math.isfinite(n) for n in box)
                    and 0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height):
                source_box = [box[0]/width, box[1]/height, box[2]/width, box[3]/height]
            if label in ("image", "footer_image", "chart"):
                figure = {"type": "figure", "content": "", "format": "text", "provider_label": label}
                from .figure_crops import matched_crop
                crop_url = matched_crop((layout.get("markdown") or {}).get("images"), box)
                if crop_url:
                    figure["provider_crop_url"] = crop_url
                if source_box:
                    figure["processed_bbox" if transformed else "source_bbox"] = source_box
                if transformed:
                    figure["coordinate_space"] = "processed"
                image_text = raw.get("image_ocr")
                if not isinstance(image_text, list):
                    image_text = ([{"text": value}] if requested.get("useOcrForImageBlock")
                        and value.strip() and not re.fullmatch(r"\s*!\[[^\]]*\]\([^)]*\)\s*", value) else [])
                if label != "chart" and image_text:
                    figure["image_ocr"] = image_text
                    figure["source_kind"] = "image_ocr"
                blocks.append(figure)
                if label != "chart":
                    continue
            if label == "chart":
                if not value.lstrip().startswith("<table"):
                    value = "<table>" + "".join("<tr>" + "".join(
                        "<td>" + escape(cell.strip()) + "</td>" for cell in line.split("|"))
                        + "</tr>" for line in value.splitlines() if line.strip()) + "</table>"
                item = {"type": "table", "format": "html", "content": value, "chart_conversion": True}
            else:
                kind = {"doc_title": "title", "paragraph_title": "title", "table": "table",
                    "display_formula": "formula", "algorithm": "code", "header": "header",
                    "footer": "footer", "number": "footer"}.get(label, "text")
                item = {"type": kind, "content": value,
                    "format": "html" if kind == "table" else "latex" if kind == "formula" else "text"}
                if source_box:
                    item["processed_bbox" if transformed else "source_bbox"] = source_box
                if label in ("seal", "seal_text"):
                    item["source_kind"] = "seal"
            if transformed:
                item["coordinate_space"] = "processed"
            blocks.append(item)
        # Keep the region and OCR evidence, but avoid indexing the same text twice.
        for figure in blocks:
            if figure.get("source_kind") != "image_ocr":
                continue
            text = "\n".join(v["text"] for v in figure["image_ocr"]
                if isinstance(v, dict) and isinstance(v.get("text"), str)).strip()
            box = figure.get("source_bbox", figure.get("processed_bbox"))
            if not text or not box:
                continue
            for index, other in enumerate(blocks):
                other_box = other.get("source_bbox", other.get("processed_bbox"))
                if other is figure or other.get("type") == "figure" or not other_box or other.get("content", "").strip() != text:
                    continue
                area = (box[2]-box[0]) * (box[3]-box[1])
                intersection = max(0, min(box[2], other_box[2])-max(box[0], other_box[0])) * max(
                    0, min(box[3], other_box[3])-max(box[1], other_box[1]))
                if intersection >= .8 * area:
                    figure["ocr_duplicate_of"] = index
                    break
        pages.append({"page_num": number, "blocks": blocks, "preprocessing": {
            "requested": requested, "reported": reported,
            "angle": angle if type(angle) in (int, float) and math.isfinite(angle) else None,
            "coordinate_space": "processed" if transformed else "original",
            "settings_confirmed": bool(reported)}})
    return {"status": "SUCCESS", "result": {"outputs": {"json": {"pages": pages}}}}


def _image_text(value):
    """Extract provider image text without accepting arbitrary nested payloads."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        for key in ("text", "content", "description", "summary"):
            if isinstance(value.get(key), str):
                return [value[key]]
        return []
    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(_image_text(item))
        return values
    return []


def _image_understanding(block, enabled):
    values = []
    for key in ("image_understanding", "image_description", "description", "understanding"):
        values.extend(_image_text(block.get(key)))
    # SoMark currently exposes figure understanding in the figure content field.
    # Only consume it when this pipeline explicitly enabled the feature.
    if not values and enabled:
        values.extend(_image_text(block.get("content")))
    return clean_text("\n".join(values))


def normalize_result(data, *, expected_pages=None, page_offset=0,
                     image_understanding_enabled=False):
    output = ((data.get("result") or {}).get("outputs") or {}).get("json")
    if isinstance(output, str):
        output = json.loads(output)
    pages = output.get("pages") if isinstance(output, dict) else None
    if not isinstance(pages, list) or not 1 <= len(pages) <= PROVIDER_MAX_PAGES:
        raise ValueError("invalid_pages")
    seen = set()
    for page in pages:
        number = page.get("page_num") if isinstance(page, dict) else None
        if type(number) is not int or not 0 <= number < PROVIDER_MAX_PAGES or number in seen:
            raise ValueError("invalid_page")
        seen.add(number)
    if seen != set(range(expected_pages if expected_pages is not None else len(pages))):
        raise ValueError("incomplete_page_coverage")
    chunks, page_results, outline = [], [], []
    for page in sorted(pages, key=lambda p: p["page_num"]):
        page_no = page_offset + page["page_num"] + 1
        blocks = page.get("blocks")
        if not isinstance(blocks, list):
            raise ValueError("invalid_blocks")
        preserved, review_reasons = [], []
        for block in blocks:
            if not isinstance(block, dict):
                raise ValueError("invalid_block")
            kind = block.get("type")
            if kind == "figure":
                value = "\n".join(item["text"] for item in block.get("image_ocr", [])
                                  if isinstance(item, dict) and isinstance(item.get("text"), str))
                if "ocr_duplicate_of" in block:
                    value = ""
                fmt = "text"
                understanding = _image_understanding(block, image_understanding_enabled)
            else:
                value = block.get("content") or ""
                fmt = block.get("format") or "text"
                understanding = ""
            if not isinstance(value, str) or len(value) > 2_000_000:
                raise ValueError("invalid_text")
            value = clean_text(value)
            source_box = block.get("source_bbox")
            has_source = (isinstance(source_box, list) and len(source_box) == 4
                and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in source_box)
                and source_box[0] < source_box[2] and source_box[1] < source_box[3])
            if len(understanding) > 2_000_000:
                raise ValueError("invalid_text")
            if not value:
                if kind != "figure":
                    continue
                item = {"type": "figure", "format": "text", "content": ""}
                if has_source:
                    item["source_bbox"] = source_box
                if understanding:
                    item["image_understanding"] = understanding
                for key in ("processed_bbox", "coordinate_space", "source_kind", "ocr_duplicate_of", "image_ocr",
                            "provider_crop_url", "provider_label", "crop", "crop_status", "crop_provider_pass"):
                    if key in block:
                        item[key] = block[key]
                preserved.append(item)
                if "figure_without_ocr" not in review_reasons:
                    review_reasons.append("figure_without_ocr")
                if understanding:
                    for offset in range(0, len(understanding), 1800):
                        chunks.append({
                            "page_no": page_no,
                            "text": "图像理解（非逐字 OCR，仅供辅助理解）："
                                    + understanding[offset:offset + 1800],
                            "kind": "image_understanding",
                        })
                        if len(chunks) > 10000:
                            raise ValueError("too_many_chunks")
                continue
            text = value
            if fmt == "html":
                table = SafeTable()
                table.feed(value)
                value = "".join(table.parts)
                parser = TableText()
                parser.feed(value)
                text = clean_text("".join(parser.text))
            item = {"type": kind if kind in ("title", "table", "formula", "figure", "text",
                                             "paragraph", "code", "list", "header", "footer") else "text",
                    "format": fmt if fmt in ("html", "latex", "markdown") else "text", "content": value}
            if has_source:
                item["source_bbox"] = source_box
            for key in ("processed_bbox", "coordinate_space", "source_kind", "ocr_duplicate_of", "image_ocr",
                        "provider_crop_url", "provider_label", "crop", "crop_status", "crop_provider_pass"):
                if key in block:
                    item[key] = block[key]
            if block.get("chart_conversion") is True:
                item["chart_conversion"] = True
            if kind == "title":
                level = block.get("level", block.get("title_level", 1))
                item["level"] = level if type(level) is int and 1 <= level <= 6 else 1
                outline.append({"title": text[:500], "page_no": page_no, "level": item["level"]})
            if kind == "figure" and understanding:
                item["image_understanding"] = understanding
            bbox = block.get("bbox")
            if (isinstance(bbox, list) and len(bbox) == 4
                    and all(type(n) in (int, float) and math.isfinite(n) for n in bbox)):
                item["bbox"] = bbox
            preserved.append(item)
            if item.get("chart_conversion"):
                text = "图表转换（非逐字原文，数值与结构需对照原图核对）：\n" + text
            for offset in range(0, len(text), 1800):
                chunk_kind = ("image_ocr" if kind == "figure"
                              else kind if kind in ("header", "footer") else "text")
                if item.get("source_kind") == "seal":
                    chunk_kind = "seal"
                if item.get("chart_conversion"):
                    chunk_kind = "image_understanding"
                chunks.append({"page_no": page_no, "text": text[offset:offset + 1800],
                               "kind": chunk_kind})
                if len(chunks) > 10000:
                    raise ValueError("too_many_chunks")
            if kind == "figure" and understanding:
                for offset in range(0, len(understanding), 1800):
                    chunks.append({
                        "page_no": page_no,
                        "text": "图像理解（非逐字 OCR，仅供辅助理解）："
                                + understanding[offset:offset + 1800],
                        "kind": "image_understanding",
                    })
                    if len(chunks) > 10000:
                        raise ValueError("too_many_chunks")
        for ordinal, item in enumerate(preserved):
            item["block_id"] = f"p{page_no}/b{ordinal}"
            item["reading_order"] = ordinal
        if page.get("preprocessing", {}).get("coordinate_space") == "processed":
            review_reasons.append("processed_coordinates_unmapped")
        page_results.append({"page_no": page_no,
                             "status": "success" if any(
                                 b["content"] or b.get("image_understanding") for b in preserved
                             ) else "empty",
                             "blocks": preserved, "review_reasons": review_reasons,
                             **({"preprocessing": page["preprocessing"]} if "preprocessing" in page else {})})
    if not chunks and expected_pages is None:
        raise ValueError("empty_result")
    return {"pages": len(pages), "chunks": chunks, "page_results": page_results, "outline": outline}


def finalize_document(state):
    pages = state["pages"]
    if not pages or any(b["status"] != "completed" for b in state["batches"]):
        raise ValueError("incomplete_batches")
    rows = sorted(state["page_results"], key=lambda p: p["page_no"])
    if [p["page_no"] for p in rows] != list(range(1, pages + 1)):
        raise ValueError("incomplete_page_coverage")
    state["page_results"] = rows
    state["chunks"].sort(key=lambda c: c["page_no"])
    state["outline"].sort(key=lambda c: c["page_no"])
    for index, heading in enumerate(state["outline"]):
        following = next((h["page_no"] for h in state["outline"][index + 1:]
                          if h["level"] <= heading["level"]), pages)
        heading["end_page"] = max(heading["page_no"], min(pages, following))
    from .study_sources import build_study_index
    state["study_index"] = build_study_index(state)
    return state


def complex_table_pages(state):
    pages = []
    for page in state.get("page_results", []):
        for block in page["blocks"]:
            if block["format"] == "html":
                parser = SafeTable()
                parser.feed(block["content"])
                if parser.max_columns > 12:
                    pages.append(page["page_no"])
                    break
    return pages


def figure_review_pages(state):
    return [p["page_no"] for p in state.get("page_results", [])
            if "figure_without_ocr" in p.get("review_reasons", [])]


def figure_understanding_pages(state):
    return [p["page_no"] for p in state.get("page_results", [])
            if any(block.get("image_understanding") for block in p.get("blocks", []))]


def document_markdown(state):
    """Export OCR plus explicitly labeled image understanding, never as raw text."""
    parts = []
    rows = state.get("page_results", [])
    if not rows:
        # Backwards compatible export of previously recognized attachments.
        for chunk in state.get("chunks", []):
            parts.append(f"\n## 原文件第 {chunk['page_no']} 页\n\n{chunk['text']}\n")
        return "\n".join(parts)
    for page in rows:
        label = (f"第 {page['page_no']} 张幻灯片" if state.get("unit") == "slide"
                 else f"原文件第 {page['page_no']} 页")
        parts.append(f"## {label}\n")
        if page.get("hidden"):
            parts.append("> 原文件中的隐藏幻灯片。\n")
        if page.get("notes"):
            parts.append("> 演讲者备注（本地提取，非 OCR）：\n\n"
                         + escape(page["notes"]) + "\n")
        if not any(b["content"] for b in page["blocks"]):
            parts.append("> 此页未返回文字；请对照原件核对是否为空白或图像页。\n")
        if "figure_without_ocr" in page.get("review_reasons", []):
            parts.append("> 此页有图片未返回图中文字；图表内知识可能缺失，请对照原件。\n")
        if "processed_coordinates_unmapped" in page.get("review_reasons", []):
            parts.append("> 页面经过方向或形变处理，区域尚未映射；图片入口显示整页原图供核对。\n")
        for block in page["blocks"]:
            content = block["content"]
            image_url = block.get("image_url", "")
            if re.fullmatch(r"/api/attachments/mat_[a-f0-9]+/images/[0-9]+/[0-9]+", image_url):
                parts.append(f"![原页图片]({image_url})\n")
            if block.get("chart_conversion"):
                parts.append("> 图表转换（非逐字原文，数值与结构需对照原图核对）：\n")
            if block.get("image_understanding"):
                parts.append("> 图像理解（非逐字 OCR，仅供辅助理解）："
                             + block["image_understanding"] + "\n")
            if not content:
                continue
            if block.get("source_kind") == "seal":
                parts.append("> 印章识别（独立资料内容）：\n")
            if block["type"] == "title":
                content = "#" * min(6, block.get("level", 1) + 2) + " " + content
            elif block["format"] == "latex" and not content.startswith(("$$", "\\[")):
                content = "$$\n" + content + "\n$$"
            # Keep OCR data inert when a Markdown reader permits embedded HTML.
            content = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", content)
            if block["format"] != "html":
                content = re.sub(r"</?[A-Za-z][^>]*>", lambda match: escape(match.group()), content)
            parts.append(content + "\n")
    return "\n".join(parts)

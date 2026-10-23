"""Local UTF-8 parsing with exact raw-line locators and bounded structure."""
from __future__ import annotations

from array import array
from bisect import bisect_left, bisect_right
import hashlib
import re

from markdown_it import MarkdownIt

from .errors import error
from .upload import decode_text

MAX_TEXT_BYTES = 20 * 1024 * 1024
MAX_TEXT_BLOCKS = 50_000
MAX_TEXT_LINES = 200_000
MAX_TEXT_HEADINGS = 5_000


def text_content(payload):
    text = decode_text(payload)
    if "\x00" in text or any(ord(c) < 32 and c not in "\r\n\t" for c in text):
        raise error("invalid_file_signature", "文本含二进制或不支持的控制字符。")
    if not text.strip():
        raise error("empty_file")
    return text


def parse_text(payload, kind, *, guard=None):
    if len(payload) > MAX_TEXT_BYTES:
        raise error("file_too_large")
    original = text_content(payload)
    source = original.replace("\r\n", "\n").replace("\r", "\n")
    if (source.count("\n") >= MAX_TEXT_LINES or
            len(re.findall(r"(?im)^(?:#{1,6}\s|chapter\s+\d+|第[一二三四五六七八九十百\d]+[章节])", source)) > MAX_TEXT_HEADINGS
            or kind == "md" and source.count("|") > 100_000):
        raise error("text_too_large", "文本结构超过处理预算，请拆分资料。")
    crlf_positions = array("I", (m.start() - i for i, m in enumerate(re.finditer("\r\n", original))))
    def raw_offset(position):
        return position + bisect_left(crlf_positions, position)
    lines = source.splitlines(keepends=True)
    offsets, cursor = [], 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    offsets.append(len(source))
    ranges = []
    if kind == "md":
        parser = MarkdownIt("commonmark", {"html": False}).enable("table")
        def token_budget(state):
            if len(state.tokens) > MAX_TEXT_BLOCKS * 4:
                raise error("text_too_large", "Markdown 结构超过处理预算，请拆分资料。")
        parser.core.ruler.before("inline", "attachment_budget", token_budget)
        tokens = parser.parse(source)
        for token in tokens:
            if token.map is None or token.level != 0 or token.type == "inline" or token.nesting == -1:
                continue
            start, end = token.map
            block_kind = {"heading_open": "title", "fence": "code", "code_block": "code",
                          "table_open": "table", "bullet_list_open": "list",
                          "ordered_list_open": "list"}.get(token.type, "text")
            ranges.append((offsets[start], offsets[min(end, len(lines))],
                           block_kind, int(token.tag[1:]) if block_kind == "title" else None))
    else:
        ranges = ((m.start(), m.end(), "text", None)
                  for m in re.finditer(r"\S[^\n]*(?:\n(?![ \t]*\n)[^\n]*)*", source))
    blocks = []
    for start, end, block_kind, level in ranges:
        while start < end:
            stop = min(end, start + 2000)
            if stop < end:
                boundary = source.rfind("\n", start + 1000, stop)
                if boundary > start:
                    stop = boundary + 1
            left = start
            while left < stop and source[left].isspace():
                left += 1
            right = stop
            while right > left and source[right - 1].isspace():
                right -= 1
            if left < right:
                content = source[left:right]
                if block_kind == "title" and kind == "md":
                    match = re.match(r"#{1,6}[ \t]+", content)
                    if match:
                        left += match.end()
                        content = source[left:right]
                block = {"type": block_kind, "format": "markdown" if kind == "md" else "text",
                         "content": content, "source_start": raw_offset(left), "source_end": raw_offset(right),
                         "line_start": bisect_right(offsets, left),
                         "line_end": bisect_right(offsets, right - 1),
                         "source_kind": "direct_text"}
                if level:
                    block["level"] = level
                blocks.append(block)
                if len(blocks) > MAX_TEXT_BLOCKS:
                    raise error("text_too_large", "文本结构过多，请拆分资料。")
            start = stop
        if guard and len(blocks) % 128 == 0:
            guard()
    # Logical segments keep existing indexed joins; they are never displayed as physical pages.
    pages = [{"page_no": i + 1, "status": "success", "blocks": [block], "review_reasons": []}
             for i, block in enumerate(blocks)]
    state = {"pipeline_version": 3, "unit": "document", "strategy": "local", "pages": 0,
             "line_count": len(lines), "raw_text_hash": hashlib.sha256(payload).hexdigest(),
             "batches": [], "page_results": pages,
             "chunks": [{"page_no": p["page_no"], "text": p["blocks"][0]["content"], "kind": "text"}
                        for p in pages],
             "outline": [{"title": b["content"][:500], "level": b["level"], "page_no": i + 1}
                         for i, b in enumerate(blocks) if b["type"] == "title"]}
    from .study_sources import build_study_index
    state["study_index"] = build_study_index(state)
    return state

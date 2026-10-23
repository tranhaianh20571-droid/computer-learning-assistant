"""Bounded OOXML inspection and relationship-complete native slide batches."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import posixpath
from urllib.parse import unquote, urlsplit
import zipfile

from lxml import etree

MAX_PPTX_BYTES = 200_000_000
MAX_PPTX_SLIDES = 300
PPTX_BATCH_TARGET = 45_000_000
PPTX_BATCH_LIMIT = 50_000_000
MAX_ENTRIES = 50_000
MAX_UNPACKED = 2_000_000_000
MAX_XML_BYTES = 16_000_000
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
PRESENTATION = "ppt/presentation.xml"


class SlideTooLarge(ValueError):
    def __init__(self, page_no):
        super().__init__("pptx_slide_too_large")
        self.page_no = page_no


def xml(data):
    if len(data) > MAX_XML_BYTES or b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ValueError("pptx_invalid")
    return etree.fromstring(data, etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False))


def encoded(root):
    return etree.tostring(root, encoding="UTF-8", xml_declaration=True, standalone=True)


def rel_path(part):
    return posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")


def target_path(part, target):
    target = unquote(target)
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.query or "\\" in target:
        raise ValueError("pptx_invalid")
    path = posixpath.normpath(posixpath.join(posixpath.dirname(part), parsed.path)
                             if not parsed.path.startswith("/") else parsed.path[1:])
    if path.startswith("../") or path in ("..", "."):
        raise ValueError("pptx_invalid")
    return path


class PresentationPackage:
    def __init__(self, source):
        self.archive = zipfile.ZipFile(source if isinstance(source, Path) else io.BytesIO(source))
        try:
            entries = self.archive.infolist()
            names = [e.filename for e in entries]
            if (len(entries) > MAX_ENTRIES or len(set(names)) != len(names)
                    or sum(e.file_size for e in entries) > MAX_UNPACKED):
                raise ValueError("pptx_invalid")
            for entry in entries:
                path = entry.filename
                if (path.startswith("/") or "\\" in path or ":" in path or
                        ".." in path.split("/") or entry.flag_bits & 1
                        or (entry.external_attr >> 16) & 0o170000 == 0o120000
                        or path.lower().endswith("vbaproject.bin")):
                    raise ValueError("pptx_invalid")
                if path.endswith((".xml", ".rels")) and entry.file_size > MAX_XML_BYTES:
                    raise ValueError("pptx_invalid")
            self.names = set(names)
            types = xml(self.archive.read("[Content_Types].xml"))
            expected = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
            if not any(e.get("PartName") == "/" + PRESENTATION and e.get("ContentType") == expected
                       for e in types):
                raise ValueError("pptx_invalid")
            self.presentation = xml(self.archive.read(PRESENTATION))
            if self.presentation.tag != f"{{{P}}}presentation":
                raise ValueError("pptx_invalid")
            self.slide_list = self.presentation.find(f"{{{P}}}sldIdLst")
            relationships = self.relationships(PRESENTATION)
            by_id = {e.get("Id"): e for e in relationships}
            if len(by_id) != len(relationships) or self.slide_list is None:
                raise ValueError("pptx_invalid")
            self.slides = []
            for number, item in enumerate(self.slide_list, 1):
                relation = by_id[item.get(f"{{{R}}}id")]
                if not relation.get("Type", "").endswith("/slide") or relation.get("TargetMode") == "External":
                    raise ValueError("pptx_invalid")
                path = target_path(PRESENTATION, relation.get("Target", ""))
                root = xml(self.archive.read(path))
                if root.tag != f"{{{P}}}sld" or any(s["part"] == path for s in self.slides):
                    raise ValueError("pptx_invalid")
                notes = []
                for link in self.relationships(path):
                    if link.get("Type", "").endswith("/notesSlide") and link.get("TargetMode") != "External":
                        note = xml(self.archive.read(target_path(path, link.get("Target", ""))))
                        for shape in note.findall(f".//{{{P}}}sp"):
                            placeholder = shape.find(f".//{{{P}}}ph")
                            if placeholder is not None and placeholder.get("type") in ("sldNum", "dt", "hdr", "ftr"):
                                continue
                            notes.extend(n.text for n in shape.findall(f".//{{{A}}}t") if n.text)
                self.slides.append({"page_no": number, "part": path,
                    "hidden": root.get("show") in ("0", "false"), "notes": "\n".join(notes)})
            if not self.slides or len(self.slides) > MAX_PPTX_SLIDES:
                raise ValueError("too_many_pages")
            # Check every relationship, including unused parts, before accepting a package.
            for path in names:
                if path.endswith(".rels"):
                    part = "" if path == "_rels/.rels" else posixpath.join(
                        posixpath.dirname(posixpath.dirname(path)), posixpath.basename(path)[:-5])
                    for relation in xml(self.archive.read(path)):
                        if relation.get("TargetMode") != "External":
                            target = target_path(part, relation.get("Target", ""))
                            if target not in self.names:
                                raise ValueError("pptx_invalid")
        except Exception:
            self.archive.close()
            raise

    def close(self):
        self.archive.close()

    def relationships(self, part):
        path = rel_path(part)
        return list(xml(self.archive.read(path))) if path in self.names else []

    def batch(self, start, end):
        selected = {s["part"] for s in self.slides[start - 1:end]}
        slide_parts = {s["part"] for s in self.slides}
        pending, kept, replacements = [PRESENTATION], set(), {}
        while pending:
            part = pending.pop()
            if part in kept:
                continue
            kept.add(part)
            root = xml(self.archive.read(part)) if part.endswith(".xml") else None
            if part == PRESENTATION:
                for item in list(root):
                    if etree.QName(item).localname in ("custShowLst", "extLst"):
                        root.remove(item)
                sequence = root.find(f"{{{P}}}sldIdLst")
                for index, item in enumerate(list(sequence), 1):
                    if not start <= index <= end:
                        sequence.remove(item)
            elif part in selected:
                root.set("show", "1")  # OCR includes hidden slides; source metadata retains their flag.
            relationships, removed_ids = [], set()
            for relation in self.relationships(part):
                if relation.get("TargetMode") == "External":
                    removed_ids.add(relation.get("Id"))
                    continue
                target = target_path(part, relation.get("Target", ""))
                if target in slide_parts and target not in selected:
                    removed_ids.add(relation.get("Id"))
                    continue
                relationships.append(relation)
                pending.append(target)
            if root is not None:
                for element in root.iter():
                    for attr, value in list(element.attrib.items()):
                        if attr.startswith(f"{{{R}}}") and value in removed_ids:
                            del element.attrib[attr]
                replacements[part] = encoded(root)
            if relationships:
                links = etree.Element(f"{{{REL}}}Relationships", nsmap={None: REL})
                links.extend(relationships)
                replacements[rel_path(part)] = encoded(links)
        types = xml(self.archive.read("[Content_Types].xml"))
        for entry in list(types):
            if entry.tag == f"{{{CT}}}Override" and entry.get("PartName", "")[1:] not in kept:
                types.remove(entry)
        root_links = etree.Element(f"{{{REL}}}Relationships", nsmap={None: REL})
        etree.SubElement(root_links, f"{{{REL}}}Relationship", Id="rId1",
                         Type=R + "/officeDocument", Target=PRESENTATION)
        replacements.update({"[Content_Types].xml": encoded(types), "_rels/.rels": encoded(root_links)})
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for part in sorted(kept | replacements.keys()):
                entry = zipfile.ZipInfo(part, date_time=(1980, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, replacements[part] if part in replacements else self.archive.read(part))
        payload = output.getvalue()
        rebuilt = PresentationPackage(payload)
        try:
            if len(rebuilt.slides) != end - start + 1:
                raise ValueError("pptx_invalid")
        finally:
            rebuilt.close()
        return payload


def inspect_pptx(source):
    package = PresentationPackage(source)
    try:
        return package.slides
    finally:
        package.close()


def pptx_batch_bytes(source, start, end):
    package = PresentationPackage(source)
    try:
        return package.batch(start, end)
    finally:
        package.close()


def plan_pptx_batches(source, *, target_bytes=None, provider_bytes=None, guard=None):
    target_bytes = PPTX_BATCH_TARGET if target_bytes is None else target_bytes
    provider_bytes = PPTX_BATCH_LIMIT if provider_bytes is None else provider_bytes
    package = PresentationPackage(source)
    batches = []
    try:
        def add(start, end, whole=False):
            if guard:
                guard()
            payload = package.batch(start, end)
            size, sha = len(payload), hashlib.sha256(payload).hexdigest()
            del payload
            if size > (provider_bytes if whole else target_bytes) and start < end:
                middle = (start + end) // 2
                add(start, middle)
                add(middle + 1, end)
            elif size > provider_bytes:
                raise SlideTooLarge(start)
            else:
                batches.append({"start": start, "end": end, "size_bytes": size,
                                "sha256": sha, "status": "pending"})
        add(1, len(package.slides), True)
        return {"pipeline_version": 3, "unit": "slide", "pages": len(package.slides),
                "strategy": "batched" if len(batches) > 1 else "direct",
                "slides": package.slides, "batches": batches, "chunks": [],
                "page_results": [], "outline": []}
    finally:
        package.close()

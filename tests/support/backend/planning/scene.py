"""结构化数据 → Excalidraw 受控场景转换层（T06）。

- 只输出白名单元素：rectangle / ellipse / text / arrow / line；
- 节点/文本/箭头元素 ID 由业务对象 ID 稳定派生（可双向映射）；
- 确定性层级布局：同层等距、父子在归属维度相邻；先修边用虚线箭头
  独立表达，不与树形归属混淆；
- 画布坐标不是业务顺序来源：业务排序只看 order_index；
- 文本按 T02 锁定常数预换行（CJK 1.0×fontSize / ASCII 0.6×fontSize，
  最多 2 行，超出截断加省略号），中文不溢出。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

from .errors import error
from .outputs import SCENE_ELEMENT_WHITELIST

# T02 锁定常数（与 scripts/slice3_font_spike.mjs / evidence 文档一致）
LAYOUT = {
    "font_family": 1,
    "font_size": 16,
    "line_height": 1.25,
    "char_w_cjk": 1.0,
    "char_w_ascii": 0.6,
    "node_inner_pad": 12,
    "stage_w": 260, "stage_h": 56,
    "course_w": 220, "course_h": 64,
    "unit_w": 240, "unit_h": 56,
    "lesson_w": 200, "lesson_h": 56,
    "column_gap": 64, "row_gap": 24,
    "margin_x": 80, "margin_y": 80,
    "max_lines": 2,
    "child_indent": 20,
}
_CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f]")
_COLORS = {
    "stage": "#e3e9ff", "course": "#fff8e0", "unit": "#e3f7e8", "lesson": "#ffffff",
    "stroke": "#1b1b1f", "prereq": "#8a8f98",
}


def _stable_id(prefix: str, business_id: str, suffix: str) -> str:
    digest = hashlib.sha1(f"{business_id}:{suffix}".encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def measure_text_width(text: str, font_size: int = LAYOUT["font_size"]) -> float:
    width = 0.0
    for ch in text:
        width += font_size * (LAYOUT["char_w_cjk"] if _CJK_RE.match(ch) else LAYOUT["char_w_ascii"])
    return width


def wrap_label(text: str, inner_width: float, max_lines: int = LAYOUT["max_lines"]) -> list[str]:
    lines: list[str] = []
    current = ""
    truncated = False
    for ch in text:
        candidate = current + ch
        if current and measure_text_width(candidate) > inner_width:
            lines.append(current)
            current = ch
            if len(lines) == max_lines:
                truncated = True
                break
        else:
            current = candidate
    if not truncated and current:
        lines.append(current)
    if truncated:
        if lines:
            lines[-1] = lines[-1][:-1] + "…" if lines[-1] else "…"
    return lines


def _text_element(business_id: str, x: float, y: float, lines: list[str]) -> dict:
    # 显式给出文本度量（restore 不为缺失度量的文本重测，宽 0 会导致不可见）
    width = max((measure_text_width(line) for line in lines), default=0.0)
    height = len(lines) * LAYOUT["font_size"] * LAYOUT["line_height"]
    return {
        "type": "text",
        "id": _stable_id("txt", business_id, "label"),
        "x": x, "y": y,
        "width": round(width, 1),
        "height": round(height, 1),
        "text": "\n".join(lines),
        "fontSize": LAYOUT["font_size"],
        "fontFamily": LAYOUT["font_family"],
        "lineHeight": LAYOUT["line_height"],
        "strokeColor": _COLORS["stroke"],
    }


def _node(business_id: str, kind: str, x: float, y: float, w: float, h: float, title: str) -> list[dict]:
    shape_type = "ellipse" if kind == "lesson" else "rectangle"
    node_id = _stable_id("node", business_id, "shape")
    pad = LAYOUT["node_inner_pad"]
    lines = wrap_label(title, w - 2 * pad)
    return [
        {
            "type": shape_type,
            "id": node_id,
            "x": x, "y": y, "width": w, "height": h,
            "strokeColor": _COLORS["stroke"],
            "backgroundColor": _COLORS.get(kind, "#ffffff"),
            "fillStyle": "solid",
        },
        _text_element(business_id, x + pad, y + 8, lines),
    ]


def _prereq_arrow(business_id: str, a: dict, b: dict) -> dict:
    ax, ay = a["x"] + a["w"] / 2, a["y"] + a["h"]
    bx, by = b["x"] + b["w"] / 2, b["y"]
    return {
        "type": "arrow",
        "id": _stable_id("arr", business_id, "prereq"),
        "x": ax, "y": ay,
        "points": [[0, 0], [bx - ax, by - ay]],
        "strokeColor": _COLORS["prereq"],
        "strokeWidth": 1,
        "strokeStyle": "dashed",
    }


def route_scene(route_version: dict) -> dict:
    """路线版本 → 场景（到课程粒度）。route_version 为 {stages:[{id,title,courses:[{id,title}]}], prerequisites:[...]}。"""
    elements: list[dict] = []
    positions: dict[str, dict] = {}
    col_w = LAYOUT["stage_w"] + LAYOUT["column_gap"]
    for i, stage in enumerate(route_version.get("stages", [])):
        sx = LAYOUT["margin_x"] + i * col_w
        sy = LAYOUT["margin_y"]
        positions[stage["id"]] = {"x": sx, "y": sy, "w": LAYOUT["stage_w"], "h": LAYOUT["stage_h"]}
        elements += _node(stage["id"], "stage", sx, sy, LAYOUT["stage_w"], LAYOUT["stage_h"], stage.get("title", ""))
        for j, course in enumerate(stage.get("courses", [])):
            cx = sx + LAYOUT["child_indent"]
            cy = sy + LAYOUT["stage_h"] + LAYOUT["row_gap"] + j * (LAYOUT["course_h"] + LAYOUT["row_gap"])
            positions[course["id"]] = {"x": cx, "y": cy, "w": LAYOUT["course_w"], "h": LAYOUT["course_h"]}
            elements += _node(course["id"], "course", cx, cy, LAYOUT["course_w"], LAYOUT["course_h"], course.get("title", ""))
    for k, edge in enumerate(route_version.get("prerequisites", [])):
        a, b = positions.get(edge.get("from")), positions.get(edge.get("to"))
        if a and b:
            elements.append(_prereq_arrow(f"edge_{k}", a, b))
    return _finalize(elements, positions)


def outline_scene(outline_version: dict) -> dict:
    """大纲版本 → 场景（到讲次粒度）。outline_version 为 {units:[{id,title,lessons:[{id,title}]}], prerequisites:[...]}。"""
    elements: list[dict] = []
    positions: dict[str, dict] = {}
    col_w = LAYOUT["unit_w"] + LAYOUT["column_gap"]
    for i, unit in enumerate(outline_version.get("units", [])):
        ux = LAYOUT["margin_x"] + i * col_w
        uy = LAYOUT["margin_y"]
        positions[unit["id"]] = {"x": ux, "y": uy, "w": LAYOUT["unit_w"], "h": LAYOUT["unit_h"]}
        elements += _node(unit["id"], "unit", ux, uy, LAYOUT["unit_w"], LAYOUT["unit_h"], unit.get("title", ""))
        for j, lesson in enumerate(unit.get("lessons", [])):
            lx = ux + LAYOUT["child_indent"] - 4
            ly = uy + LAYOUT["unit_h"] + LAYOUT["row_gap"] + j * (LAYOUT["lesson_h"] + LAYOUT["row_gap"])
            positions[lesson["id"]] = {"x": lx, "y": ly, "w": LAYOUT["lesson_w"], "h": LAYOUT["lesson_h"]}
            elements += _node(lesson["id"], "lesson", lx, ly, LAYOUT["lesson_w"], LAYOUT["lesson_h"], lesson.get("title", ""))
    for k, edge in enumerate(outline_version.get("prerequisites", [])):
        a, b = positions.get(edge.get("from")), positions.get(edge.get("to"))
        if a and b:
            elements.append(_prereq_arrow(f"edge_{k}", a, b))
    return _finalize(elements, positions)


def draft_scene(kind: str, content: dict) -> dict:
    """草稿候选（RouteCandidate/OutlineCandidate 形状，扁平索引先修、无业务 id）→ 场景。

    合成稳定键 stage_{i} / stage_{i}course_{j}（大纲同理）；
    先修索引越界静默忽略（与确认路径的受控校验不同，预览不阻塞）。
    """
    if kind in ("route", "route_generate"):
        stages: list[dict] = []
        course_key_by_index: list[str] = []
        for i, stage in enumerate(content.get("stages", [])):
            courses = []
            for j, course in enumerate(stage.get("courses", [])):
                key = f"stage_{i}course_{j}"
                course_key_by_index.append(key)
                courses.append({"id": key, "title": course.get("title", "")})
            stages.append({"id": f"stage_{i}", "title": stage.get("title", ""), "courses": courses})
        prerequisites = [
            {"from": course_key_by_index[e["from_index"]], "to": course_key_by_index[e["to_index"]]}
            for e in content.get("prerequisites", [])
            if isinstance(e, dict) and isinstance(e.get("from_index"), int) and isinstance(e.get("to_index"), int)
            and 0 <= e["from_index"] < len(course_key_by_index) and 0 <= e["to_index"] < len(course_key_by_index)
        ]
        return route_scene({"stages": stages, "prerequisites": prerequisites})
    if kind in ("course_outline", "outline_generate"):
        units: list[dict] = []
        lesson_key_by_index: list[str] = []
        for i, unit in enumerate(content.get("units", [])):
            lessons = []
            for j, lesson in enumerate(unit.get("lessons", [])):
                key = f"unit_{i}lesson_{j}"
                lesson_key_by_index.append(key)
                lessons.append({"id": key, "title": lesson.get("title", "")})
            units.append({"id": f"unit_{i}", "title": unit.get("title", ""), "lessons": lessons})
        prerequisites = [
            {"from": lesson_key_by_index[e["from_index"]], "to": lesson_key_by_index[e["to_index"]]}
            for e in content.get("prerequisites", [])
            if isinstance(e, dict) and isinstance(e.get("from_index"), int) and isinstance(e.get("to_index"), int)
            and 0 <= e["from_index"] < len(lesson_key_by_index) and 0 <= e["to_index"] < len(lesson_key_by_index)
        ]
        return outline_scene({"units": units, "prerequisites": prerequisites})
    raise error("invalid_field", f"unknown draft kind {kind}")


def _finalize(elements: list[dict], positions: dict) -> dict:
    for el in elements:
        if el["type"] not in SCENE_ELEMENT_WHITELIST:
            raise error("ROUTE_STRUCTURE_INVALID", f"element type {el['type']} not allowed")
    node_to_business = {
        _stable_id("node", bid, "shape"): bid for bid in positions
    }
    return {
        "elements": elements,
        "node_positions": positions,
        "node_to_business": node_to_business,
        "layout": dict(LAYOUT),
    }


def validate_scene(scene: dict) -> None:
    for el in scene.get("elements", []):
        if el.get("type") not in SCENE_ELEMENT_WHITELIST:
            raise error("ROUTE_STRUCTURE_INVALID", "scene element outside whitelist")


def business_sort_key(item: dict) -> tuple:
    return (item.get("unit_index", item.get("stage_index", 0)), item.get("order_index", 0))


__all__ = [
    "LAYOUT", "SCENE_ELEMENT_WHITELIST", "route_scene", "outline_scene", "draft_scene",
    "validate_scene", "wrap_label", "measure_text_width", "business_sort_key",
]

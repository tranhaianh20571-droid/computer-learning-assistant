"""结构化数据 → Excalidraw 受控场景转换层（T06，v2 横向缩进树布局）。

- 只输出白名单元素：rectangle / ellipse / text / arrow / line；
- 节点/文本/箭头元素 ID 由业务对象 ID 稳定派生（可双向映射）；
- 横向缩进树布局（tidy tree）：根在左、子级右缩、父节点按子树垂直居中；
  同层子节点按先修关系拓扑稳定排序（Kahn 稳定变体，只影响画布纵序）；
- 归属关系用浅色肘线（line）表达；先修边用虚线箭头独立表达：
  同列无遮挡走直线，其余经右侧外环走廊正交折线，不穿节点；
- 画布坐标不是业务顺序来源：业务排序只看 order_index；
- 文本按 T02 锁定常数预换行（CJK 1.0×fontSize / ASCII 0.6×fontSize），
  标题/目标双字号（16/12），course/lesson 卡片为白底圆角 + 层级色 accent 条。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

from .errors import error
from .outputs import SCENE_ELEMENT_WHITELIST

# 字体度量常数为 T02 锁定值（与 scripts/slice3_font_spike.mjs / evidence 文档一致）；
# 几何常数为 v2 横向缩进树布局所用
LAYOUT = {
    "font_family": 1,
    "font_size": 16,
    "desc_size": 12,
    "line_height": 1.25,
    "char_w_cjk": 1.0,
    "char_w_ascii": 0.6,
    "node_inner_pad": 12,
    "stage_w": 220, "unit_w": 220,
    "course_w": 300, "lesson_w": 300,
    "level_gap": 72,
    "row_gap": 18,
    "margin_x": 48, "margin_y": 48,
    "max_lines": 2,
    "desc_max_lines": 2,
    "accent_w": 5,
    "min_h_parent": 48,
    "min_h_child": 56,
    "lane_gap": 28,
    "lane_step": 12,
}
_CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef\u3000-\u303f]")
# 卡片化配色：stage/unit 为浅底分组头，course/lesson 为白卡 + accent 条
_COLORS = {
    "stage": {"fill": "#eef0ff", "accent": "#7a5af8"},
    "course": {"fill": "#ffffff", "accent": "#e8930c"},
    "unit": {"fill": "#e9f7f0", "accent": "#12946a"},
    "lesson": {"fill": "#ffffff", "accent": "#2f6fdb"},
    "card_stroke": "#d7dce3",
    "parent_stroke": "#c9cfdd",
    "title": "#1b1b1f",
    "desc": "#6b7280",
    "link": "#c3c8d0",
    "prereq": "#8a8f98",
}


def _stable_id(prefix: str, business_id: str, suffix: str) -> str:
    digest = hashlib.sha1(f"{business_id}:{suffix}".encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def measure_text_width(text: str, font_size: int = LAYOUT["font_size"]) -> float:
    width = 0.0
    for ch in text:
        width += font_size * (LAYOUT["char_w_cjk"] if _CJK_RE.match(ch) else LAYOUT["char_w_ascii"])
    return width


def wrap_label(
    text: str, inner_width: float, max_lines: int = LAYOUT["max_lines"], font_size: int = LAYOUT["font_size"]
) -> list[str]:
    lines: list[str] = []
    current = ""
    truncated = False
    for ch in text:
        candidate = current + ch
        if current and measure_text_width(candidate, font_size) > inner_width:
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


def _text_element(
    business_id: str, x: float, y: float, lines: list[str], *,
    suffix: str = "label", font_size: Optional[int] = None, color: Optional[str] = None,
) -> dict:
    # 显式给出文本度量（restore 不为缺失度量的文本重测，宽 0 会导致不可见）
    fs = font_size if font_size is not None else LAYOUT["font_size"]
    width = max((measure_text_width(line, fs) for line in lines), default=0.0)
    height = len(lines) * fs * LAYOUT["line_height"]
    return {
        "type": "text",
        "id": _stable_id("txt", business_id, suffix),
        "x": x, "y": y,
        "width": round(width, 1),
        "height": round(height, 1),
        "text": "\n".join(lines),
        "fontSize": fs,
        "fontFamily": LAYOUT["font_family"],
        "lineHeight": LAYOUT["line_height"],
        "strokeColor": color or _COLORS["title"],
    }


def _node(
    business_id: str, kind: str, x: float, y: float, w: float, title: str, goal: str = ""
) -> tuple[list[dict], int]:
    """卡片节点：圆角白卡 + 左侧层级 accent 条 + 标题/目标双字号文本。

    stage/unit 为浅底分组头（无 accent 条）。返回 (元素列表, 节点高度)，
    高度随文本行数确定，供纵向堆叠使用。
    """
    pad = LAYOUT["node_inner_pad"]
    is_parent = kind in ("stage", "unit")
    accent_w = 0 if is_parent else LAYOUT["accent_w"]
    inner = w - 2 * pad - accent_w
    title_lines = wrap_label(title, inner)
    desc_lines = wrap_label(goal, inner, LAYOUT["desc_max_lines"]) if goal else []
    title_h = len(title_lines) * LAYOUT["font_size"] * LAYOUT["line_height"]
    desc_h = len(desc_lines) * LAYOUT["desc_size"] * LAYOUT["line_height"]
    content_h = title_h + (4 + desc_h if desc_lines else 0)
    min_h = LAYOUT["min_h_parent"] if is_parent else LAYOUT["min_h_child"]
    h = max(min_h, round(content_h + 2 * pad))
    colors = _COLORS[kind]
    text_y = y + (h - content_h) / 2
    elements: list[dict] = [
        {
            "type": "rectangle",
            "id": _stable_id("node", business_id, "shape"),
            "x": x, "y": y, "width": w, "height": h,
            "strokeColor": _COLORS["parent_stroke"] if is_parent else _COLORS["card_stroke"],
            "backgroundColor": colors["fill"],
            "fillStyle": "solid",
            "strokeWidth": 1,
            "roundness": {"type": 2, "value": 8},
        }
    ]
    text_x = x + pad + accent_w
    if not is_parent:
        elements.append(
            {
                "type": "rectangle",
                "id": _stable_id("acc", business_id, "bar"),
                "x": x, "y": y, "width": accent_w, "height": h,
                "strokeColor": colors["accent"],
                "backgroundColor": colors["accent"],
                "fillStyle": "solid",
                "strokeWidth": 1,
                "roundness": {"type": 2, "value": 2},
            }
        )
    elements.append(_text_element(business_id, text_x, text_y, title_lines))
    if desc_lines:
        elements.append(
            _text_element(
                business_id, text_x, text_y + title_h + 4, desc_lines,
                suffix="desc", font_size=LAYOUT["desc_size"], color=_COLORS["desc"],
            )
        )
    return elements, h


def _belonging_link(business_id: str, parent_pos: dict, child_pos: dict) -> dict:
    """归属肘线：父右缘中点 → 中缝垂直 → 子左缘中点（无箭头，浅色实线）。"""
    px = parent_pos["x"] + parent_pos["w"]
    py = parent_pos["y"] + parent_pos["h"] / 2
    cx = child_pos["x"]
    cy = child_pos["y"] + child_pos["h"] / 2
    mid_x = px + LAYOUT["level_gap"] / 2
    return {
        "type": "line",
        "id": _stable_id("lnk", business_id, "link"),
        "x": px, "y": py,
        "points": [[0, 0], [mid_x - px, 0], [mid_x - px, cy - py], [cx - px, cy - py]],
        "strokeColor": _COLORS["link"],
        "strokeWidth": 1,
        "roundness": {"type": 3},
    }


def _blocked_midway(positions: dict[str, dict], a: dict, b: dict) -> bool:
    """同列直线先修边（a 底 → b 顶）是否会被其他节点穿过。"""
    top = a["y"] + a["h"]
    bottom = b["y"]
    if top >= bottom:
        return True
    x0, x1 = a["x"], a["x"] + a["w"]
    for pos in positions.values():
        if pos is a or pos is b:
            continue
        if pos["x"] + pos["w"] <= x0 or pos["x"] >= x1:
            continue
        if pos["y"] < bottom and pos["y"] + pos["h"] > top:
            return True
    return False


def _prereq_arrow(business_id: str, a: dict, b: dict, positions: dict[str, dict], lane_x: float) -> dict:
    """先修箭头：同列无遮挡 → 底中到顶中直线；否则右侧外环走廊正交折线。"""
    base = {
        "type": "arrow",
        "id": _stable_id("arr", business_id, "prereq"),
        "strokeColor": _COLORS["prereq"],
        "strokeWidth": 1,
        "strokeStyle": "dashed",
        "roundness": {"type": 3},
        "endArrowhead": "arrow",
    }
    if a["x"] == b["x"] and a["y"] + a["h"] < b["y"] and not _blocked_midway(positions, a, b):
        ax, ay = a["x"] + a["w"] / 2, a["y"] + a["h"]
        base.update({"x": ax, "y": ay, "points": [[0, 0], [0, b["y"] - ay]]})
        return base
    ax, ay = a["x"] + a["w"], a["y"] + a["h"] / 2
    bx, by = b["x"] + b["w"], b["y"] + b["h"] / 2
    base.update(
        {
            "x": ax, "y": ay,
            "points": [[0, 0], [lane_x - ax, 0], [lane_x - ax, by - ay], [bx - ax, by - ay]],
        }
    )
    return base


def _topological_rank(children: list[dict], edges: list[tuple]) -> dict[str, int]:
    """子节点按先修边做稳定拓扑排序（Kahn，每步取原始序最小者）；成环退回原序。

    返回 id → 画布纵向秩。仅用于排布，不影响业务 order_index。
    """
    ids = [c["id"] for c in children]
    index = {nid: i for i, nid in enumerate(ids)}
    succ: dict[str, list[str]] = {nid: [] for nid in ids}
    indeg = {nid: 0 for nid in ids}
    for f, t in edges:
        if f in index and t in index and f != t:
            succ[f].append(t)
            indeg[t] += 1
    ready = sorted([nid for nid in ids if indeg[nid] == 0], key=index.__getitem__)
    order: list[str] = []
    while ready:
        nid = ready.pop(0)
        order.append(nid)
        for t in succ[nid]:
            indeg[t] -= 1
            if indeg[t] == 0:
                ready.append(t)
                ready.sort(key=index.__getitem__)
    if len(order) != len(ids):
        return {c["id"]: i for i, c in enumerate(children)}
    return {nid: i for i, nid in enumerate(order)}


def _hier_scene(
    roots: list[dict], kinds: list[str], widths: list[int],
    prereq_pairs: list[tuple], prereq_depth: int,
) -> dict:
    """N 层横向缩进树（v3）：roots=[{id,title,goal?,children:[...]}] 递归排布。

    depth d 使用 kinds[d]/widths[d]；父节点按子树垂直居中；归属肘线逐级连接；
    prereq_pairs 为 prereq_depth 层节点间 (from_id, to_id) 边，越界端点静默忽略
    （与草稿预览的容错口径一致），先修层子节点按边做稳定拓扑排序。
    """
    elements: list[dict] = []
    positions: dict[str, dict] = {}
    x_by_depth: list[float] = []
    x = LAYOUT["margin_x"]
    for w in widths:
        x_by_depth.append(x)
        x += w + LAYOUT["level_gap"]
    max_right = x_by_depth[-1] + widths[-1]

    def collect(node: dict, depth: int, out: list[dict]) -> None:
        if depth == prereq_depth:
            out.append(node)
            return
        for c in node.get("children") or []:
            collect(c, depth + 1, out)

    prereq_nodes: list[dict] = []
    for r in roots:
        collect(r, 0, prereq_nodes)
    rank = _topological_rank(prereq_nodes, prereq_pairs)

    def ordered_children(node: dict, depth: int) -> list[dict]:
        children = node.get("children") or []
        if depth + 1 == prereq_depth and len(children) > 1:
            return sorted(children, key=lambda c: rank.get(c["id"], 0))
        return children

    def place(node: dict, depth: int, y: float) -> dict:
        kind, w = kinds[depth], widths[depth]
        x = x_by_depth[depth]
        boxes: list[dict] = []
        cursor = y
        for c in ordered_children(node, depth):
            boxes.append(place(c, depth + 1, cursor))
            cursor = boxes[-1]["y"] + boxes[-1]["h"] + LAYOUT["row_gap"]
        _, ph = _node(node["id"], kind, x, 0, w, node.get("title", ""), node.get("goal", ""))
        if boxes:
            span_top = boxes[0]["y"]
            span_bottom = boxes[-1]["y"] + boxes[-1]["h"]
            py = round((span_top + span_bottom) / 2 - ph / 2)
        else:
            py = y
            span_bottom = y + ph
        els, _ = _node(node["id"], kind, x, py, w, node.get("title", ""), node.get("goal", ""))
        elements.extend(els)
        positions[node["id"]] = {"x": x, "y": py, "w": w, "h": ph}
        for b in boxes:
            elements.append(_belonging_link(b["id"], positions[node["id"]], positions[b["id"]]))
        return {"id": node["id"], "y": py, "h": ph, "bottom": max(span_bottom, py + ph)}

    y = LAYOUT["margin_y"]
    for root in roots:
        placed = place(root, 0, y)
        y = placed["bottom"] + LAYOUT["row_gap"]
    for k, (f, t) in enumerate(prereq_pairs):
        a, b = positions.get(f), positions.get(t)
        if a is None or b is None:
            continue
        lane_x = max_right + LAYOUT["lane_gap"] + k * LAYOUT["lane_step"]
        elements.append(_prereq_arrow(f"edge_{k}", a, b, positions, lane_x))
    return _finalize(elements, positions)


def route_scene(route_version: dict) -> dict:
    """路线版本 → 场景（大标题→课程→单元→讲次；课程无单元时该支只到课程）。

    route_version 为 {title, stages:[{id,title,courses:[{id,title,goal?,
    units:[{id,title,lessons:[{id,title,goal?}]}]}]}], prerequisites:[{from,to}]}。
    阶段仅承载分组数据不单独渲染；课程跨阶段按原顺序拍平。
    """
    root_children = []
    for s in route_version.get("stages", []):
        for c in s.get("courses", []):
            units = []
            for k, u in enumerate(c.get("units") or []):
                units.append({
                    "id": u.get("id") or f"{c['id']}u{k}",
                    "title": u.get("title", ""),
                    "children": [
                        {
                            "id": l.get("id") or f"{c['id']}u{k}l{j}",
                            "title": l.get("title", ""),
                            "goal": l.get("goal", ""),
                        }
                        for j, l in enumerate(u.get("lessons") or [])
                    ],
                })
            root_children.append({
                "id": c["id"], "title": c.get("title", ""), "goal": c.get("goal", ""),
                "children": units,
            })
    root = {"id": "route_root", "title": route_version.get("title", "") or "学习路线", "children": root_children}
    pairs = [(e.get("from"), e.get("to")) for e in route_version.get("prerequisites", [])]
    return _hier_scene(
        [root], ["stage", "course", "unit", "lesson"],
        [LAYOUT["stage_w"], LAYOUT["course_w"], LAYOUT["unit_w"], LAYOUT["lesson_w"]],
        pairs, prereq_depth=1,
    )


def outline_scene(outline_version: dict) -> dict:
    """大纲版本 → 场景（课程→单元→讲次）。outline_version 为
    {title,units:[{id,title,lessons:[{id,title,goal?}]}],prerequisites:[{from,to}]}。"""
    units = [
        {
            "id": u["id"], "title": u.get("title", ""),
            "children": [
                {"id": l["id"], "title": l.get("title", ""), "goal": l.get("goal", "")}
                for l in u.get("lessons", [])
            ],
        }
        for u in outline_version.get("units", [])
    ]
    root = {"id": "course_root", "title": outline_version.get("title", "") or "课程", "children": units}
    pairs = [(e.get("from"), e.get("to")) for e in outline_version.get("prerequisites", [])]
    return _hier_scene(
        [root], ["course", "unit", "lesson"],
        [LAYOUT["course_w"], LAYOUT["unit_w"], LAYOUT["lesson_w"]],
        pairs, prereq_depth=2,
    )


def draft_scene(kind: str, content: dict, title: str = "") -> dict:
    """草稿候选（RouteCandidate/OutlineCandidate 形状，扁平索引先修、无业务 id）→ 场景。

    合成稳定键 stage_{i} / stage_{i}course_{j} / …course_{j}unit_{k} / …lesson_{l}
    （大纲同理）；goal 随卡片透传；先修索引越界静默忽略（与确认路径的受控
    校验不同，预览不阻塞）。title 为目标对象标题（大标题/课程根卡回退用）。
    """
    if kind in ("route", "route_generate"):
        root_children: list[dict] = []
        course_key_by_index: list[str] = []
        for i, stage in enumerate(content.get("stages", [])):
            for j, course in enumerate(stage.get("courses", [])):
                key = f"stage_{i}course_{j}"
                course_key_by_index.append(key)
                units = []
                for k, unit in enumerate(course.get("units") or []):
                    ukey = f"{key}unit_{k}"
                    lessons = [
                        {
                            "id": f"{ukey}lesson_{l}",
                            "title": lesson.get("title", ""),
                            "goal": lesson.get("goal", ""),
                        }
                        for l, lesson in enumerate(unit.get("lessons") or [])
                    ]
                    units.append({"id": ukey, "title": unit.get("title", ""), "lessons": lessons})
                root_children.append({
                    "id": key, "title": course.get("title", ""), "goal": course.get("goal", ""),
                    "units": units,
                })
        root_title = content.get("title") or title or "学习路线"
        prerequisites = [
            {"from": course_key_by_index[e["from_index"]], "to": course_key_by_index[e["to_index"]]}
            for e in content.get("prerequisites", [])
            if isinstance(e, dict) and isinstance(e.get("from_index"), int) and isinstance(e.get("to_index"), int)
            and 0 <= e["from_index"] < len(course_key_by_index) and 0 <= e["to_index"] < len(course_key_by_index)
        ]
        return route_scene({"title": root_title, "stages": [
            {"id": "stage_all", "title": "", "courses": root_children}
        ], "prerequisites": prerequisites})
    if kind in ("course_outline", "outline_generate"):
        units: list[dict] = []
        lesson_key_by_index: list[str] = []
        for i, unit in enumerate(content.get("units", [])):
            lessons = []
            for j, lesson in enumerate(unit.get("lessons", [])):
                key = f"unit_{i}lesson_{j}"
                lesson_key_by_index.append(key)
                lessons.append({"id": key, "title": lesson.get("title", ""), "goal": lesson.get("goal", "")})
            units.append({"id": f"unit_{i}", "title": unit.get("title", ""), "lessons": lessons})
        prerequisites = [
            {"from": lesson_key_by_index[e["from_index"]], "to": lesson_key_by_index[e["to_index"]]}
            for e in content.get("prerequisites", [])
            if isinstance(e, dict) and isinstance(e.get("from_index"), int) and isinstance(e.get("to_index"), int)
            and 0 <= e["from_index"] < len(lesson_key_by_index) and 0 <= e["to_index"] < len(lesson_key_by_index)
        ]
        return outline_scene({
            "title": content.get("title") or title or "课程",
            "units": units, "prerequisites": prerequisites,
        })
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

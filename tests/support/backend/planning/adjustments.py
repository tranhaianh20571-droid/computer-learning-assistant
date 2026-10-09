"""Stable draft references, typed local edits and server-computed differences."""
from copy import deepcopy
from typing import Literal
import uuid
from pydantic import BaseModel, Field
from .errors import error
from .outputs import validate_content_json


class DialogueAnswer(BaseModel):
    action: Literal["clarification", "revision_request", "explanation"]
    answer: str = Field(min_length=1, max_length=2000)
    options: list[str] = Field(default_factory=list, max_length=6)
    instruction: str = Field(default="", max_length=4000)


class NodeOperation(BaseModel):
    op: Literal["update", "add", "delete", "move", "link", "unlink"]
    node_ref: str = Field(default="", max_length=80)
    parent_ref: str = Field(default="", max_length=80)
    index: int = Field(default=0, ge=0, le=120)
    title: str | None = Field(default=None, max_length=120)
    goal: str | None = Field(default=None, max_length=120)
    from_ref: str = Field(default="", max_length=80)
    to_ref: str = Field(default="", max_length=80)


class RevisionAnswer(BaseModel):
    summary: str = Field(min_length=1, max_length=1500)
    operations: list[NodeOperation] = Field(min_length=1, max_length=100)


def nodes(content, kind):
    result = []
    root_role = "route" if kind == "route" else "course"
    def walk(node, role, parent, siblings, path):
        result.append((node, role, parent, siblings, path))
        key = {"route": "stages", "stage": "courses", "course": "units", "unit": "lessons"}.get(role)
        child_role = {"route": "stage", "stage": "course", "course": "unit", "unit": "lesson"}.get(role)
        if key:
            for i, child in enumerate(node.get(key, [])):
                suffix = {"stages": "stage_", "courses": "course_", "units": "unit_", "lessons": "lesson_"}[key]
                child_path = f"{path}{suffix}{i}" if path not in ("route_root", "course_root") else f"{suffix}{i}"
                walk(child, child_role, node, node[key], child_path)
    walk(content, root_role, None, None, f"{root_role}_root")
    return result


def ensure_refs(content, kind):
    seen = set()
    for node, *_ in nodes(content, kind):
        ref = node.get("node_ref")
        if not isinstance(ref, str) or not ref or ref in seen or len(ref) > 80:
            ref = f"n_{uuid.uuid4().hex[:16]}"
            node["node_ref"] = ref
        seen.add(ref)
    return content


def node_catalog(content, kind):
    return [{"node_ref": n["node_ref"], "title": n.get("title", ""), "role": role,
             "parent_ref": p["node_ref"] if p else "", "path": path}
            for n, role, p, _, path in nodes(content, kind)]


def _members(node, role):
    if role == "route":
        return [c for s in node.get("stages", []) for c in s.get("courses", [])]
    return [l for u in node.get("units", []) for l in u.get("lessons", [])]


def apply_operations(base, kind, operations):
    content = ensure_refs(deepcopy(base), kind)
    edges = {}
    for n, role, *_ in nodes(content, kind):
        if role in ("route", "course"):
            members = _members(n, role)
            edges[n["node_ref"]] = [(members[e["from_index"]]["node_ref"], members[e["to_index"]]["node_ref"])
                for e in n.get("prerequisites", [])
                if 0 <= e["from_index"] < len(members) and 0 <= e["to_index"] < len(members)]
    for op in operations:
        op = NodeOperation.model_validate(op)
        lookup = {n["node_ref"]: (n, role, p, siblings) for n, role, p, siblings, _ in nodes(content, kind)}
        if op.op in ("link", "unlink"):
            parent = lookup.get(op.parent_ref)
            if not parent or parent[1] not in ("route", "course"):
                raise error("INVALID_PREREQUISITE")
            refs = {n["node_ref"] for n in _members(parent[0], parent[1])}
            if op.from_ref not in refs or op.to_ref not in refs:
                raise error("INVALID_PREREQUISITE")
            pair = (op.from_ref, op.to_ref)
            bucket = edges.setdefault(op.parent_ref, [])
            if op.op == "link" and pair not in bucket: bucket.append(pair)
            if op.op == "unlink" and pair in bucket: bucket.remove(pair)
            continue
        if op.op == "add":
            parent = lookup.get(op.parent_ref)
            if not parent or parent[1] == "lesson" or not op.title or op.node_ref in lookup:
                raise error("OUTPUT_INVALID")
            if not op.node_ref.startswith("new_"):
                raise error("OUTPUT_INVALID")
            child_key = {"route": "stages", "stage": "courses", "course": "units", "unit": "lessons"}[parent[1]]
            node = {"node_ref": op.node_ref, "title": op.title}
            if parent[1] in ("stage", "unit"): node["goal"] = op.goal or ""
            next_key = {"route": "courses", "stage": "units", "course": "lessons"}.get(parent[1])
            if next_key: node[next_key] = []
            parent[0].setdefault(child_key, []).insert(op.index, node)
            continue
        item = lookup.get(op.node_ref)
        if not item:
            raise error("OUTPUT_INVALID")
        node, role, parent, siblings = item
        if op.op == "update":
            if op.title is not None:
                if not op.title.strip(): raise error("OUTPUT_INVALID")
                node["title"] = op.title.strip()
            if op.goal is not None: node["goal"] = op.goal.strip()
        elif op.op == "delete":
            if siblings is None: raise error("OUTPUT_INVALID")
            siblings.remove(node)
        elif op.op == "move":
            destination = lookup.get(op.parent_ref)
            expected = {"stage": "route", "course": "stage", "unit": "course", "lesson": "unit"}.get(role)
            if siblings is None or not destination or destination[1] != expected:
                raise error("OUTPUT_INVALID")
            # Moving lessons/units across courses changes their content ownership.
            if role in ("unit", "lesson"):
                def owner(ref):
                    current = lookup[ref]
                    while current[1] != "course": current = lookup[current[2]["node_ref"]]
                    return current[0]["node_ref"]
                if owner(op.node_ref) != owner(op.parent_ref): raise error("OUTPUT_INVALID")
            siblings.remove(node)
            key = {"stage": "stages", "course": "courses", "unit": "units", "lesson": "lessons"}[role]
            destination[0].setdefault(key, []).insert(op.index, node)
    for n, role, *_ in nodes(content, kind):
        if role in ("route", "course"):
            refs = {m["node_ref"]: i for i, m in enumerate(_members(n, role))}
            n["prerequisites"] = [{"from_index": refs[a], "to_index": refs[b]}
                for a, b in edges.get(n["node_ref"], []) if a in refs and b in refs]
    validate_content_json(kind, content)
    return content


def differences(base, result, kind):
    before = {n["node_ref"]: (n, p["node_ref"] if p else "", i)
              for i, (n, _, p, _, _) in enumerate(nodes(base, kind))}
    after = {n["node_ref"]: (n, p["node_ref"] if p else "", i)
             for i, (n, _, p, _, _) in enumerate(nodes(result, kind))}
    changes = []
    for ref in before.keys() | after.keys():
        a, b = before.get(ref), after.get(ref)
        if not a: action = "added"
        elif not b: action = "removed"
        elif (a[0].get("title"), a[0].get("goal")) != (b[0].get("title"), b[0].get("goal")): action = "updated"
        elif a[1:] != b[1:]: action = "moved"
        else: continue
        changes.append({"node_ref": ref, "action": action, "title": (b or a)[0].get("title", "")})
    def dependency_pairs(content):
        result = {}
        for n, role, *_ in nodes(content, kind):
            if role in ("route", "course"):
                refs = [m["node_ref"] for m in _members(n, role)]
                result[n["node_ref"]] = {(refs[e["from_index"]], refs[e["to_index"]])
                    for e in n.get("prerequisites", [])}
        return result
    old_edges, new_edges = dependency_pairs(base), dependency_pairs(result)
    for ref in old_edges.keys() & new_edges.keys():
        if old_edges[ref] != new_edges[ref]:
            changes.append({"node_ref": ref, "action": "prerequisite", "title": after[ref][0].get("title", "")})
    return sorted(changes, key=lambda item: (item["action"], item["node_ref"]))

"""Bounded continuation of a frozen question search, with durable receipts."""
from copy import deepcopy

from .study_sources import digest, source_candidates

POLICY = (
    "source_candidates是本轮检索结果，不等于资料全部题目。"
    "与当前目标不匹配或完整原题不足时，可调用search_more_source_questions定向补取；"
    "只检索冻结选定资料，不能把未检索到写成教材没有题。"
    "工具共享当前任务预算，最多三轮，每轮最多五题；返回新候选加入本次可引用ID。"
)


def question_search(db, owner, snapshot, lessons, candidates, *, state, guard, save,
                    excluded=(), purpose="assessment", feynman=False):
    signature = digest([snapshot, lessons, sorted(excluded), purpose, feynman])
    if state and state.get("signature") != signature:
        raise ValueError("question_search_scope_changed")
    if not state:
        state.update(signature=signature, rounds=0, candidates=[], receipts=[])
        save()
    known = {q["id"] for q in candidates}
    candidates.extend(deepcopy(q) for q in state["candidates"] if q["id"] not in known)

    def search_more_source_questions(query: str) -> dict:
        """Retrieve up to five additional questions relevant to the current frozen lesson."""
        guard()
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
            return {"status": "invalid_query", "candidates": []}
        if state["rounds"] >= 3:
            return {"status": "budget_exhausted", "candidates": []}
        state["rounds"] += 1
        receipt = {"round": state["rounds"], "status": "processing"}
        state["receipts"].append(receipt)
        save()
        with db.session() as session:
            found = source_candidates(session, owner, snapshot,
                [{**lesson, "goal": query} for lesson in lessons],
                include_examples=True, feynman=feynman, purpose=purpose, limit=5,
                exclude_ids={*excluded, *(q["id"] for q in candidates)}, receipt=receipt)
        guard()
        candidates.extend(found)
        state["candidates"].extend(deepcopy(found))
        receipt["status"] = "completed" if found else "no_new_candidates"
        save()
        return {"status": receipt["status"], "candidates": found, "round": state["rounds"]}
    return search_more_source_questions


def conversation_question_search(service, deps, conv_id, task_id, lease, snapshot, lessons,
                                 candidates, *, excluded=(), purpose="teaching", feynman=False):
    """Private checkpoints live with the existing owned lesson, outside frozen scope fields."""
    import json

    from ..planning.models import load_json_field
    from ..teaching.learning_models import LessonBlueprintRow
    with service.db.session() as session:
        row = session.get(LessonBlueprintRow, conv_id)
        if row is None or row.owner_user_id != deps.actor_id:
            return None
        stored = load_json_field(row.context_json, {})
        state = deepcopy(stored.get("_question_searches", {}).get(task_id, {}))

    def save():
        deps.check()
        with service.db.session() as session:
            if lease is not None:
                service.tasks.validate_lease(lease, actor_id=deps.actor_id, session=session)
            row = session.get(LessonBlueprintRow, conv_id, with_for_update=True)
            if not row or row.owner_user_id != deps.actor_id:
                raise ValueError("question_search_owner_changed")
            current = load_json_field(row.context_json, {})
            if current.get("source_snapshot", {}) != snapshot:
                raise ValueError("question_search_scope_changed")
            current.setdefault("_question_searches", {})[task_id] = state
            row.context_json = json.dumps(current, ensure_ascii=False)
    return question_search(service.db, deps.actor_id, snapshot, lessons, candidates, state=state,
        guard=deps.check, save=save, excluded=excluded, purpose=purpose, feynman=feynman)

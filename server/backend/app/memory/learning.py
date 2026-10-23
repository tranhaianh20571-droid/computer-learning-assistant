"""Trusted, versioned learning evidence and clock-dependent estimates."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC

from fsrs import Card, Rating, Scheduler
from sqlalchemy import func, select

from .models import LearningEvidenceRow, MemorySettingsRow, now

ALGORITHM = "fsrs-6.3.2-default-v1"
DESIRED_RETENTION = 0.9
SCHEDULER = Scheduler(enable_fuzzing=False)


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def identity(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def record(session, owner, course, event_id, items, *, occurred_at, sources):
    """Only a publishing business transaction may supply event identity and help state."""
    setting = session.get(MemorySettingsRow, owner, with_for_update=True)
    if not setting or not setting.enabled or owner.startswith("guest_") or not any(sources):
        return
    if aware(occurred_at) > now():
        raise ValueError("future learning evidence")
    grouped = {}
    for item in items:
        concept = item.get("concept_id")
        version = item.get("content_version")
        if not isinstance(concept, str) or not concept or not isinstance(version, str) or not version:
            continue
        if len(concept) > 100 or len(version) > 100:
            raise ValueError("learning identity too long")
        key = (concept, version)
        grouped.setdefault(key, []).append(item)
    for (concept, version), values in sorted(grouped.items()):
        ident = identity(owner, course, event_id, concept, version)
        existing = session.get(LearningEvidenceRow, ident, with_for_update=True)
        if existing:
            # A repeat delivery or a changed grade is not a second study session.
            continue
        independence = ("assisted" if any(v.get("independence") == "assisted" for v in values)
                        else "independent" if all(v.get("independence") == "independent" for v in values)
                        else "unknown")
        outcome = ("incorrect" if any(v.get("result") == "incorrect" for v in values)
                   else "correct" if all(v.get("result") == "correct" for v in values)
                   else "unknown")
        session.add(LearningEvidenceRow(evidence_id=ident, owner_user_id=owner, course_id=course,
            concept_id=concept, content_version=version,
            lesson_id=str(values[0].get("lesson_id", ""))[:64],
            occurred_at=aware(occurred_at), independence=independence, result=outcome,
            source_ids_json=json.dumps(sorted({s for s in sources if s}))))


def estimate(rows, at):
    at = aware(at)
    reviews = sorted((r for r in rows if r.status == "active" and r.independence == "independent"
                      and r.result in ("correct", "incorrect") and aware(r.occurred_at) <= at),
                     key=lambda r: (aware(r.occurred_at), r.evidence_id))
    base = {"status": "unknown", "retrievability": None, "action": "diagnose",
            "independent_count": len(reviews), "last_verified_date": None,
            "last_correct": None, "uncalibrated": True}
    if not reviews:
        return base
    card = None
    for row in reviews:
        card, _ = SCHEDULER.review_card(card or Card(card_id=1),
            Rating.Good if row.result == "correct" else Rating.Again,
            review_datetime=aware(row.occurred_at))
    last = reviews[-1]
    correct = last.result == "correct"
    probability = SCHEDULER.get_card_retrievability(card, current_datetime=at)
    return {**base, "status": "estimated" if correct else "needs_relearning",
            "retrievability": round(probability, 6) if correct else None,
            "action": "review" if not correct else "advance" if probability >= DESIRED_RETENTION else "diagnose",
            "last_verified_date": aware(last.occurred_at).date().isoformat(),
            "last_correct": correct,
            "elapsed_days": round((at - aware(last.occurred_at)).total_seconds() / 86400, 3)}


def select_review_targets(state, topics, *, limit=3):
    """Select exact, verified concepts for question priority; never grades or deletes."""
    if not isinstance(state, dict) or state.get("status") != "ready" or not isinstance(topics, dict):
        return None
    candidates = []
    for item in state.get("items", []):
        if not isinstance(item, dict):
            continue
        concept_id = item.get("concept_id")
        topic = topics.get(concept_id)
        count = item.get("independent_count")
        correct = item.get("last_correct")
        if (not isinstance(concept_id, str) or not concept_id or not isinstance(topic, str) or not topic.strip()
                or type(count) is not int or count < 1 or correct is not True and correct is not False):
            continue
        retrieval = item.get("retrievability")
        if item.get("action") == "review" and correct is False:
            priority, action, retrieval_order = 0, "review", -1.0
        elif (item.get("action") == "diagnose" and correct is True
              and isinstance(retrieval, (int, float)) and not isinstance(retrieval, bool)
              and 0 <= retrieval < DESIRED_RETENTION):
            priority, action, retrieval_order = 1, "recall_check", float(retrieval)
        else:
            continue
        elapsed = item.get("elapsed_days")
        elapsed_order = -float(elapsed) if isinstance(elapsed, (int, float)) else 0.0
        candidates.append((priority, retrieval_order, elapsed_order, concept_id, {
                "concept_id": concept_id,
                "topic": topic.strip()[:120],
                "action": action,
                "retrievability": retrieval if action == "recall_check" else None,
                "last_verified_date": item.get("last_verified_date"),
            }))
    if not candidates:
        return None
    candidates.sort(key=lambda candidate: candidate[:4])
    return {"algorithm": state.get("algorithm", ALGORITHM),
        "as_of": state.get("as_of", ""),
        "uncalibrated": True,
        "items": [candidate[4] for candidate in candidates[:max(1, min(int(limit), 3))]]}


def context(session, owner, course, *, at=None, limit=8, lesson="", content_version=""):
    from ..auth.models import UserRow
    from ..planning.models import CourseRow
    at = aware(at or now())
    setting = session.get(MemorySettingsRow, owner)
    user = session.get(UserRow, owner)
    row = session.get(CourseRow, course)
    if (not setting or not setting.enabled or not user or user.status != "approved"
            or not row or row.deleted_at or row.owner_user_id != owner):
        return {"status": "unavailable", "items": []}
    query = select(LearningEvidenceRow).where(
        LearningEvidenceRow.owner_user_id == owner, LearningEvidenceRow.course_id == course,
        LearningEvidenceRow.status == "active", LearningEvidenceRow.occurred_at <= at)
    if lesson:
        query = query.where(LearningEvidenceRow.lesson_id == lesson)
    if content_version:
        query = query.where(LearningEvidenceRow.content_version == content_version)
    limit = max(1, min(int(limit), 128))
    keys = session.execute(select(LearningEvidenceRow.concept_id, LearningEvidenceRow.content_version)
        .where(*query.whereclause.clauses).group_by(
            LearningEvidenceRow.concept_id, LearningEvidenceRow.content_version)
        .order_by(func.max(LearningEvidenceRow.occurred_at).desc(),
                  LearningEvidenceRow.concept_id, LearningEvidenceRow.content_version).limit(limit)).all()
    items = []
    for concept, version in keys:
        values = list(session.scalars(query.where(LearningEvidenceRow.concept_id == concept,
            LearningEvidenceRow.content_version == version)
            .order_by(LearningEvidenceRow.occurred_at.desc(), LearningEvidenceRow.evidence_id).limit(10001)))
        state = estimate(values, at) if len(values) <= 10000 else {
            **estimate([], at), "reason": "history_limit", "independent_count": None}
        items.append({"concept_id": concept, "content_version": version,
                      "lesson_id": values[0].lesson_id, **state})
    return {"status": "ready", "as_of": at.date().isoformat(), "algorithm": ALGORITHM,
            "items": items, "boundary": "程序估计未经校准；未知不等于零，未作答不等于失败。"
            "提示后成功不代表独立掌握；不同内容版本不得沿用掌握度。"
            "低提取率先诊断，不能宣称已实际遗忘。"}


def revoke(session, owner, *, source_id=None, course=None):
    query = select(LearningEvidenceRow).where(LearningEvidenceRow.owner_user_id == owner,
                                            LearningEvidenceRow.status == "active")
    if course is not None:
        query = query.where(LearningEvidenceRow.course_id == course)
    for row in session.scalars(query.with_for_update()):
        if source_id and source_id not in json.loads(row.source_ids_json):
            continue
        row.status, row.result, row.independence = "revoked", "unknown", "unknown"
        row.concept_id = row.content_version = row.lesson_id = ""
        row.source_ids_json = "[]"
        row.updated_at = now()


def correct_grade(session, owner, evidence_id, *, result, expected_revision):
    """A trusted grading correction replaces the old result without another review."""
    from ..planning.errors import error
    row = session.get(LearningEvidenceRow, evidence_id, with_for_update=True)
    if not row or row.owner_user_id != owner or row.status != "active":
        raise error("resource_not_found", status=404)
    if row.grading_revision != expected_revision or result not in ("correct", "incorrect", "unknown"):
        raise error("state_invalid", status=409)
    row.result, row.grading_revision = result, expected_revision + 1
    row.updated_at = now()


def record_assessment(session, owner, assessment, attempt, result, sources):
    questions = json.loads(assessment.questions_json or "{}").get("questions", [])
    grades = {g["question_id"]:g for g in result.get("grades", [])}
    items = []
    for question in questions:
        grade = grades.get(question["id"])
        if not grade:
            continue
        # Current assessment protocol does not attest whether outside help was used.
        for concept in question.get("knowledge_point_ids", []):
            items.append({"concept_id": concept, "content_version": assessment.outline_version_id,
                "lesson_id": question.get("primary_lesson_id", ""),
                "independence": "unknown", "result":
                    "correct" if grade.get("earned") == grade.get("maximum")
                    else "incorrect" if grade.get("earned") == 0 else "unknown"})
    record(session, owner, assessment.course_id, attempt.attempt_id, items,
           occurred_at=attempt.created_at, sources=sources)


def record_feynman(session, owner, course, turn_id, occurred_at, payload, sources):
    if payload.get("phase") != "transfer":
        return
    result = payload.get("learning_result")
    concepts = sorted(set(payload.get("knowledge_point_ids", [])))
    mapped = len(concepts) == 1
    items = [{"concept_id": concept, "content_version": payload.get("content_version", ""),
              "lesson_id": payload.get("lesson_id", ""),
              "independence": "unknown" if not mapped else "assisted" if payload.get("assisted") is True else
                  "independent" if payload.get("assisted") is False else "unknown",
              "result": "correct" if result == "understood" else
                  "incorrect" if result == "review" else "unknown"}
             for concept in concepts]
    record(session, owner, course, turn_id, items, occurred_at=occurred_at, sources=sources)

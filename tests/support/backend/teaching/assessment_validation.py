"""Deterministic assessment checks, not heuristics pretending to judge semantics."""
from collections import Counter
from decimal import Decimal, InvalidOperation
import re

from .assessment_contract import LessonQuota


def reference_numbers(text):
    """Read bounded numeric literals and fractions without evaluating expressions."""
    numbers = [Decimal(v) for v in re.findall(
        r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", text)]
    fractions = re.findall(r"([-+]?\d+(?:\.\d+)?)\s*/\s*([-+]?\d+(?:\.\d+)?)", text)
    fractions += re.findall(r"\\(?:d?frac)\{([-+]?\d+(?:\.\d+)?)\}\{([-+]?\d+(?:\.\d+)?)\}", text)
    numbers.extend(Decimal(a) / Decimal(b) for a, b in fractions if Decimal(b) != 0)
    return numbers


def quotas_for(lessons):
    quotas = []
    for lesson in lessons:
        count = max(1, len(lesson.get("scope_points") or lesson.get("knowledge_points", [])))
        advanced = (False if lesson.get("scope_points") is not None else
            any(step.get("phase") in ("misconception", "transfer")
                for step in lesson.get("taught", [])))
        core = count >= 3 or advanced or lesson.get("is_prerequisite", False)
        importance = "core" if core else "normal" if count == 2 else "light"
        target = min(12, 10 + max(0, count - 3)) if core else 8 if count == 2 else 5
        basis = "冻结考纲知识点" if lesson.get("scope_points") is not None else "已讲知识点"
        quotas.append(LessonQuota(lesson_id=lesson["lesson_id"], importance=importance,
            target_questions=target, rationale=f"{basis} {count} 个；"
            f"纠错或迁移阶段：{'有' if advanced else '无'}；"
            f"后续先修：{'是' if lesson.get('is_prerequisite') else '否'}。").model_dump())
    return quotas


def content_issues(output, context):
    issues = []
    quotas = context["lesson_quotas"]
    if [q.model_dump() for q in output.lesson_quotas] != quotas:
        issues.append("必须原样使用服务端冻结的 lesson_quotas")
    actual = Counter(q.primary_lesson_id for q in output.questions)
    if actual != Counter({q["lesson_id"]: q["target_questions"] for q in quotas}):
        issues.append("每讲实际题数必须等于冻结题量，不能跨讲重复计数")
    lessons = {l["lesson_id"]: l for l in context["lessons"]}
    seen = {re.sub(r"\s+", "", q["prompt"]).casefold() for q in context.get("prior_questions", [])}
    scoped_points = set()
    scoped_coverage = set()
    for q in output.questions:
        lesson = lessons.get(q.primary_lesson_id)
        if lesson is None or q.primary_lesson_id not in q.lesson_ids:
            issues.append("primary_lesson_id 必须属于本讲且包含于 lesson_ids")
            continue
        allowed_points = {p["id"] for p in lesson.get("knowledge_points", [])}
        if lesson.get("scope_points") is not None:
            scoped_points.update(allowed_points)
            scoped_coverage.update(q.knowledge_point_ids)
        if not set(q.knowledge_point_ids) <= allowed_points:
            issues.append("知识点引用不属于冻结考纲范围")
        normalized = re.sub(r"\s+", "", q.prompt).casefold()
        if normalized in seen:
            issues.append("题干完全重复，须提供有练习价值的不同任务")
        seen.add(normalized)
        figure = q.figure
        catalog = lesson.get("figure_catalog", {"status": "absent", "figures": []})
        known = {f["ref"] for f in catalog["figures"]}
        if figure.kind == "material_figure" and figure.ref not in known:
            issues.append("图引用不属于本讲冻结资料")
        if figure.kind == "material_figure" and any(
                item["ref"] == figure.ref and item.get("content_status") == "unavailable"
                for item in catalog["figures"]):
            issues.append("资料图只有裁剪位置、没有可读内容依据，不能确认与题干一致；"
                          "请补齐原图理解依据或改用条件完整且与题干相符的自含图")
        if q.source_use != "original":
            visible = q.prompt + "\n" + figure.caption
            if re.search(r"(?i)the answer is|最终答案[为是：:]|exp\s*\([^)]*\)\s*[≈=]\s*[-\d]", visible):
                issues.append("题干或图注泄露最终答案或最后一步")
        if q.numeric_answer and q.numeric_answer.comparison == "equal":
            number = q.numeric_answer
            if number.notation != "exact":
                try:
                    expected = Decimal(str(number.value)).quantize(Decimal(1).scaleb(-number.decimals))
                    # A numeric reference can include units or an equality prefix.
                    step = Decimal(1).scaleb(-number.decimals)
                    literals = reference_numbers(" ".join(q.answer))
                    if not any(value.quantize(step) == expected for value in literals):
                        issues.append("数值参考答案必须包含按 rounding 精度取整后的值")
                except InvalidOperation:
                    issues.append("数值参考答案无法按指定精度表示")
    if scoped_points - scoped_coverage:
        issues.append("测验尚未覆盖冻结考纲中的全部知识点")
    for lesson in lessons.values():
        catalog = lesson.get("figure_catalog", {})
        if catalog.get("status") == "unavailable":
            issues.append("教材有图但无法提取；必须先修复原图，不能当作无图")
        if catalog.get("status") == "available" and not any(
                q.primary_lesson_id == lesson["lesson_id"] and q.figure.kind in (
                    "material_figure", "generated_chart", "generated_figure") for q in output.questions):
            issues.append("本讲教材有图，至少一道题须实际依赖图作答")
    return issues

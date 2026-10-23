"""Versioned delivery policy: grounded usability is separate from research depth."""
from .evidence import evidence_checks,evidence_summary,unmeasured_style_numbers


def acceptance_quality(verdict,sources,policy,subject,extraction,skill,research_failures):
    summary=evidence_summary(sources)
    original=subject.get("kind")=="original"
    material=any(item.get("kind")=="user_material" and item.get("text","").strip() for item in sources)
    checks=[item.model_dump() for item in verdict.checks]
    checks.extend([
        {"name":"source_basis","passed":original or material or summary["effective_documents"]>0,
         "reason":"原创明确标记作者设定；已有角色必须有实际有效资料或用户提供的材料。"},
        {"name":"style_statistics","passed":not unmeasured_style_numbers(skill+"\n"+extraction.expression_dna),
         "reason":"不得将未测量的表达比例或句长写成实测。"},
        {"name":"model_boundaries","passed":len(extraction.boundaries)>=3 and len(extraction.values_and_tensions)>=2,
         "reason":"保留至少三条具体边界和两项价值张力。"},
    ])
    richness=evidence_checks(sources,policy)
    if subject.get("kind")=="fictional":
        richness=[
            {"name":"canonical_material","passed":material or summary["verified_primary"]>0,
             "reason":"优先原作或官方设定；一本原作可以支持多个场景，不按网页篇数凑量。"},
            richness[2]]
    warnings=[item["reason"] for item in richness if not item["passed"]] if not original else []
    warnings.extend(f"{key}研究未完成：{code}" for key,code in research_failures.items())
    warnings.extend(verdict.weaknesses)
    passed=all(item["passed"] for item in checks)
    return {"schema_version":2,"passed":passed,
        "grade":("authored" if original else "grounded" if not warnings else "limited") if passed else "draft",
        "warnings":list(dict.fromkeys(warnings)),
        "report":{"checks":checks,"weaknesses":verdict.weaknesses},
        "research_depth":{"checks":richness,"evidence":summary},"default_eligible":False}

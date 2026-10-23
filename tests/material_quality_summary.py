"""Aggregate private quality probes without copying textbook or answer content."""
import argparse
import json
from collections import Counter
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", nargs="+", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    private = (root / ".backup").resolve()
    destination = (root / args.out).resolve()
    if not destination.is_relative_to(private):
        parser.error("Output must remain private.")
    result = {"lessons": [], "failures": [], "blind_questions": 0}
    for location in args.runs:
        folder = (root / location).resolve()
        if not folder.is_relative_to(private):
            parser.error("All inputs must remain private.")
        run = json.loads((folder / "run.json").read_text(encoding="utf8"))
        stages = run["stages"]
        result["failures"].extend({"run": folder.name, **failure}
                                  for failure in run.get("failures", []))
        for name, stage in stages.items():
            if name.startswith("blind_exam_"):
                result["blind_questions"] += len(stage["output"]["questions"])
        indices = sorted({key.rsplit("_", 1)[0] for key in stages
                          if key.endswith("_concept")})
        for ident in indices:
            concept = stages.get(ident + "_concept", {}).get("output", {})
            example = stages.get(ident + "_example", {}).get("output", {})
            feynman = stages.get(ident + "_feynman", {}).get("output", {})
            assessment = stages.get(ident + "_assessment_reviewed", {}).get("output", {})
            steps = [*concept.get("steps", []), *example.get("steps", [])]
            examples = [item for step in steps for item in step.get("examples", [])]
            used = {item["source_question_id"] for item in examples if item["source_question_id"]}
            originals = [q for q in assessment.get("questions", []) if q["source_use"] == "original"]
            blocks = {block["id"]: block for step in steps for block in [
                *step["board"].get("blocks", []),
                *(block for beat in step.get("beats", []) for block in beat.get("blocks", []))]}
            metrics = run.get("material_metrics", {}).get(ident, {})
            result["lessons"].append({"run": folder.name, "lesson_id": ident,
                "title": metrics.get("title") or (steps[0]["title"] if steps else ""),
                "segments": len(steps), "examples": len(examples),
                "example_sources": dict(Counter(item["source_use"] for item in examples)),
                "board_blocks": dict(Counter(block["type"] for block in blocks.values())),
                "feynman": bool(feynman), "feynman_source_use": feynman.get("source_use"),
                "questions": len(assessment.get("questions", [])),
                "question_sources": dict(Counter(q["source_use"] for q in assessment.get("questions", []))),
                "unused_originals": len([q for q in originals if q["source_question_id"] not in used]),
                "repeated_originals": len([q for q in originals if q["source_question_id"] in used]),
                "question_figures": dict(Counter(q.get("figure", {}).get("kind", "none")
                                                for q in assessment.get("questions", []))),
                "metrics": {k: v for k, v in metrics.items() if k != "examples"}})
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    lines = ["# 真实材料探针汇总", "",
        "各讲只生成概念与应用代表段，不代表整讲学习完成；失败及未生成阶段不计通过。", "",
        "测验题数只统计结构与变式审查阶段的产物，不等同于题图或内容终验通过。", "",
        "| 批次/讲次 | 标题 | 短段 | 例题 | 费曼 | 结构/变式阶段题数 | 未讲原题 | 重复原题 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for lesson in result["lessons"]:
        lines.append(f"| {lesson['run']}/{lesson['lesson_id']} | {lesson['title']} | "
                     f"{lesson['segments']} | {lesson['examples']} | {lesson['feynman']} | "
                     f"{lesson['questions']} | {lesson['unused_originals']} | {lesson['repeated_originals']} |")
    lines += ["", f"无资料考纲出题：{result['blind_questions']} 道。",
              f"保留历史失败记录：{len(result['failures'])} 条；历史失败与最终已通过阶段可能重叠，不相加作为失败率。"]
    (destination / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf8")
    print(json.dumps({"lessons": len(result["lessons"]),
        "reviewed_questions": sum(item["questions"] for item in result["lessons"]),
        "blind_questions": result["blind_questions"], "historical_failures": len(result["failures"])}))


if __name__ == "__main__":
    main()

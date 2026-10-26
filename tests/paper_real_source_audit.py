"""Read-only source audit against an explicitly selected, running owned desktop."""
import argparse
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import psutil


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--conversation", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    processes = [p for p in psutil.process_iter(["cmdline"]) if
        "app.desktop_child" in (p.info["cmdline"] or []) and "api" in p.info["cmdline"]]
    if len(processes) != 1:
        raise RuntimeError("Select exactly one running owned desktop")
    os.environ.update(processes[0].environ())
    os.environ["CONTENT_TRACE_ENABLED"] = "0"
    sys.path.insert(0, str(root / "server/backend"))
    from sqlalchemy import func, select
    from app.logging_audit.db import Database
    from app.materials.storage import MaterialStorage
    from app.planning.models import ConversationRow, CourseRow
    from app.teaching.learning_models import LessonBlueprintRow
    from app.teaching.source_reading import build_source_reading_plan, pdf_paragraphs
    from app.teaching.interactive import source_path

    service = SimpleNamespace(db=Database(), storage=MaterialStorage(os.environ["MATERIALS_STORAGE_DIR"]))
    try:
        with service.db.session() as session:
            conversation = session.get(ConversationRow, args.conversation)
            if conversation is None:
                raise RuntimeError("Selected conversation does not exist")
            session.expunge(conversation)
            context = json.loads(session.get(LessonBlueprintRow, args.conversation).context_json)
            courses = session.scalar(select(func.count()).select_from(CourseRow))
            conversations = session.scalar(select(func.count()).select_from(ConversationRow))
        plan = build_source_reading_plan(service, conversation, context)
        document = context["source_snapshot"]["documents"][0]
        original = source_path(service, conversation.owner_user_id, context["source_snapshot"],
            document["material_id"], document["sha256"])
        native_captions = [{"page": number, "text": p["text"][:140], "bbox": p["bbox"]}
            for number in (2, 4) for p in pdf_paragraphs(original, document["material_id"], document["sha256"], number)
            if "Figure" in p["text"] or "Fig." in p["text"]]
        result = {
            "plan_version": plan.version, "units": len(plan.units),
            "first_page": plan.units[0].target["page_no"],
            "opening_units": sum(u.knowledge_point_id == "paper_opening" for u in plan.units),
            "opening_has_title": "Dream-RSI" in plan.units[0].source_text,
            "opening_scope_credit": sum(len(u.scope_capability_ids) for u in plan.units if u.knowledge_point_id == "paper_opening"),
            "figure_targets": [{"figure_no": t["figure_no"], "page_no": t["page_no"],
                "precision": t["precision"], "bbox": t.get("bbox"),
                "material_id": t["material_id"], "sha256": t.get("sha256")} for t in plan.figure_targets],
            "courses": courses, "conversations": conversations, "provider_calls": 0,
            "native_captions": native_captions,
        }
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
    finally:
        service.db.dispose()


if __name__ == "__main__":
    main()

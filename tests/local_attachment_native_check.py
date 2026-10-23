"""Inspect local source binding in the native browser's owned schema only."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server/backend"))


def main():
    import json
    from sqlalchemy import text
    from app.logging_audit.db import Database
    from app.slice3_app import Slice3State
    from app.materials.study_sources import documents, chapter_catalog, freeze_sources, source_candidates
    from app.planning.retrieval import PlanningRetrievalService
    db = Database()
    try:
        with db.session() as session:
            schema = session.scalar(text("SELECT current_schema()"))
            if not schema.startswith("course_library_validation_") or len(schema) != 58:
                raise RuntimeError("refusing source inspection outside owned browser schema")
        Slice3State(db)
        owner, ids = sys.argv[1], sys.argv[2:]
        with db.session() as session:
            docs = documents(session, owner, ids)
            catalog = chapter_catalog(docs)
            assert len(docs) == 2 and all(chapter["unit"] == "document" for chapter in catalog)
            snapshot = freeze_sources(session, owner, ids, [
                ("synthetic-lesson", {"source_section_ids": [chapter["id"] for chapter in catalog]})])
            candidates = source_candidates(session, owner, snapshot, [{"lesson_id": "synthetic-lesson"}])
            assert candidates and all(c["unit"] == "document" and c["line_start"] > 0 for c in candidates)
        retrieval = PlanningRetrievalService(db).assemble(
            owner_user_id=owner, material_ids=ids, query="sampling")
        assert retrieval.excerpts and all(e["unit"] == "document" for e in retrieval.excerpts)
        print(json.dumps({"documents": len(docs), "candidates": len(candidates),
                          "retrieval_hits": len(retrieval.excerpts), "line_locators": True}))
    finally:
        db.dispose()


if __name__ == "__main__":
    main()

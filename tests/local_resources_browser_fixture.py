"""Seed and advance local storage jobs only inside the owned browser schema."""
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"server/backend"))
from sqlalchemy import select,text
from app.logging_audit.db import Database
from app.slice3_app import Slice3State
from app.cleanup.manager import LocalResourceManager
from app.logging_audit.repositories import PgAuditLog
from app.materials.models import MaterialRow

db=Database()
try:
    with db.session() as session:
        schema=session.scalar(text("SELECT current_schema()"))
        if not schema.startswith("course_library_validation_") or len(schema)!=58:
            raise RuntimeError("owned schema required")
    state=Slice3State(db,PgAuditLog(db))
    manager=LocalResourceManager(state)
    action=sys.argv[1]
    if action=="seed":
        from app.auth.models import UserRow
        with db.session() as session:
            owner=session.scalar(select(UserRow.user_id).where(UserRow.is_admin.is_(True)))
            material=MaterialRow(material_id="mat_abcdef123456",owner_user_id=owner,kind="txt",size_bytes=9,
                source_bytes_sha256="synthetic",filename_hash="synthetic",status="parsed")
            session.add(material)
        key=state.storage.original_key("mat_abcdef123456")
        state.storage.put(key,b"synthetic")
        manager.observe(state.storage,key)
    else:
        for _ in range(10):
            manager.tick()
    print(json.dumps({"ok":True}))
finally:
    db.dispose()

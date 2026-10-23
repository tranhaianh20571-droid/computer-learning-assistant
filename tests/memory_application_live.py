"""Explicit real engine + durable application delivery, using synthetic owned data."""
import argparse
import asyncio
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "backend"))


def verify():
    from sqlalchemy import select
    from app.auth.models import UserRow
    from app.db import Database
    from app.memory.models import MemorySettingsRow, MemorySourceRow, MemorySpaceRow
    from app.memory.service import MemoryService, enqueue
    with tempfile.TemporaryDirectory(prefix="synthetic-memory-") as temporary:
        db = Database("sqlite+pysqlite:///" + (Path(temporary) / "test.db").as_posix())
        service = MemoryService(db, None)
        for model in (UserRow, MemorySettingsRow, MemorySpaceRow, MemorySourceRow):
            model.__table__.create(db.engine)
        owner = "synthetic_" + uuid.uuid4().hex[:16]
        with db.session() as session:
            session.add(UserRow(user_id=owner, email=owner + "@example.com",
                password_hash="synthetic-unused", status="approved", role="member"))
        service.configure(owner, True, 0)
        with db.session() as session:
            enqueue(session, owner, "chat_user_statement", "synthetic-profile",
                {"role": "user", "content": "我是软件工程师，喜欢逐步解释和生活化例子，不喜欢牵强的职业比喻。"})
        bank = None
        try:
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                service.tick()
                with db.session() as session:
                    source = session.scalar(select(MemorySourceRow))
                    bank = session.scalar(select(MemorySpaceRow.bank_id))
                    status = source.status
                if status == "completed":
                    break
                if status == "failed":
                    raise AssertionError("native operation failed")
                time.sleep(2)
            assert status == "completed", "native model completion deadline"
            profile = asyncio.run(service.read(owner))
            assert profile["status"] == "ready"
            data = profile["data"]
            rows = data.get("items", data.get("mental_models", []))
            assert rows and any(row.get("content") for row in rows), "empty native model"
            print("PASS: opt-in, durable source, native retain, operation completion, nonempty profile.")
        finally:
            if bank:
                asyncio.run(service.adapter.delete_bank(bank))
                print("Owned synthetic application bank cleanup PASS")
            db.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live required")
    try:
        verify()
    except Exception as exc:
        print("application memory verification FAIL", type(exc).__name__)
        raise SystemExit(1) from None

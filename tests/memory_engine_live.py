"""Opt-in official-engine verification using an owned synthetic bank only."""
import argparse
import asyncio
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "backend"))


async def verify():
    from app.memory.adapter import HindsightAdapter
    adapter = HindsightAdapter()
    bank = "learning-validation-" + uuid.uuid4().hex
    try:
        await adapter.retain(bank, "profile-source",
            "用户明确表示喜欢逐步解释和买菜排队等日常生活例子。用户职业是软件工程师。",
            "profile", "2026-10-18T00:00:00Z")
        print("native retain PASS", flush=True)
        await adapter.retain(bank, "card-a-source",
            "当前用户对人物卡A明确反馈：减少古风术语，保留温和语气；例子优先日常生活。",
            "character:synthetic-A", "2026-10-18T00:00:01Z")
        await adapter.retain(bank, "card-b-source",
            "当前用户对人物卡B明确反馈：保持简练直接，避免幽默。只属于人物B。",
            "character:synthetic-B", "2026-10-18T00:00:02Z")
        result = await adapter.recall(bank, "character:synthetic-A", "用户希望人物怎样讲解？")
        data = result.model_dump(mode="json")
        facts = data.get("results", [])
        assert facts, "empty recall"
        assert not any("人物B" in f.get("text", "") for f in facts)
        print("strict character recall PASS", flush=True)
        result = await adapter.reflect(bank, "profile", "用户的背景和讲解偏好是什么？")
        assert result.model_dump(mode="json").get("text")
        print("native reflect PASS", flush=True)
        await adapter.refresh(bank, "profile")
        print("native mental model creation accepted", flush=True)
        await adapter.delete_document(bank, "card-a-source", "character:synthetic-A")
        client = adapter.client()
        try:
            remaining = await client.alist_memories(bank, document_id="card-a-source")
            data = remaining.model_dump(mode="json")
            assert not data.get("items", [])
        finally:
            await client.aclose()
        print("native document deletion PASS", flush=True)
    finally:
        await adapter.delete_bank(bank)
        print("owned synthetic bank cleanup PASS", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live required")
    try:
        asyncio.run(asyncio.wait_for(verify(), timeout=420))
    except Exception as exc:
        print("engine verification FAIL", type(exc).__name__)
        raise SystemExit(1) from None

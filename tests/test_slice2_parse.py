"""切片 2 T03/T04 测试：逐页解析、OCR 调度、原图裁切、坐标逆变换。

真实 PaddleOCR 凭据为人工前置（T01 外部阻断）；本文件用合成适配器驱动
整条管线，锁定行为契约。
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys_path = str(ROOT / "tests" / "support")
if sys_path not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path)

from backend.logging_audit.db import get_database_url  # noqa: E402
import os  # noqa: E402

os.environ.setdefault("LEARNING_DATABASE_URL", get_database_url())

from backend.logging_audit.db import Database  # noqa: E402
from backend.logging_audit.models import install_audit_triggers  # noqa: E402
from backend.logging_audit.repositories import PgTaskEventStore  # noqa: E402
from backend.materials.crop import CropService, map_bbox_to_pixels  # noqa: E402
from backend.materials.ocr import OcrPipelineService, normalize_ocr_regions  # noqa: E402
from backend.materials.parse import page_count  # noqa: E402
from backend.materials.storage import MaterialStorage  # noqa: E402
from backend.materials.upload import MaterialService, QuotaService, validate_one  # noqa: E402
from backend.tasks.service import TaskService  # noqa: E402


class SyntheticOcrAdapter:
    """合成 OCR：可编程区域结果；记录 submit 调用。"""

    def __init__(self, *, blocks=None, fail_submit=False) -> None:
        self.blocks = blocks if blocks is not None else [
            {"block_bbox": [60, 60, 400, 200], "block_label": "text",
             "block_content": "扫描页合成 OCR 文本", "block_index": 0},
            {"block_bbox": [60, 240, 400, 480], "block_label": "structure_diagram",
             "block_content": "", "block_index": 1},
        ]
        self.fail_submit = fail_submit
        self.submit_calls: list[tuple[str, int]] = []

    def submit(self, *, material_id: str, page_no: int, options=None) -> dict:
        if self.fail_submit:
            exc = Exception("timeout")
            exc.code = "timeout"
            raise exc
        self.submit_calls.append((material_id, page_no))
        return {"external_request_id": f"job_{material_id}_{page_no}", "status": "submitted"}

    def fetch_result(self, external_request_id: str) -> dict:
        return {
            "external_request_id": external_request_id,
            "result": {"layoutParsingResults": [{"prunedResult": {"parsing_res_list": self.blocks}}]},
        }


@pytest.fixture(scope="module")
def database() -> Database:
    db = Database()
    db.create_all()
    install_audit_triggers(db.engine)
    return db


@pytest.fixture()
def env(database: Database, tmp_path):
    storage = MaterialStorage(base_dir=tmp_path / "storage")
    tasks = TaskService(database, PgTaskEventStore(database))
    events = PgTaskEventStore(database)
    from backend.materials.chunk_index import ChunkIndexService

    adapter_holder = {"adapter": None}
    index = ChunkIndexService(database)
    pipeline = OcrPipelineService(
        database, tasks, events, index, storage=storage,
        ocr_adapter_factory=lambda owner, task_id: adapter_holder["adapter"],
    )
    materials = MaterialService(database, QuotaService(database), storage=storage)
    return {
        "db": database, "storage": storage, "tasks": tasks, "events": events,
        "index": index, "pipeline": pipeline, "materials": materials,
        "adapter": adapter_holder,
    }


def _pdf(pages: int = 2, with_text: bool = False) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for i in range(pages):
        if with_text:
            c.drawString(60, 760, f"Neural network chapter {i + 1} conv2d_block_7 v1.2.3")
            c.drawString(60, 720, "Transaction isolation and concurrency control body text for trust evaluation.")
        c.showPage()
    c.save()
    return buf.getvalue()


def _make_material(env, owner: str, payload: bytes, kind: str = "pdf") -> str:
    v = validate_one(filename=f"doc.{kind}", content_type="application/pdf" if kind == "pdf" else "text/plain",
                     payload=payload)
    return env["materials"].create_material(owner_user_id=owner, validated=v)["material_id"]


class TestTrustedTextPath:
    def test_trusted_page_skips_ocr(self, env) -> None:
        owner = f"user_{uuid.uuid4().hex[:8]}"
        mid = _make_material(env, owner, _pdf(pages=1, with_text=True))
        adapter = SyntheticOcrAdapter()
        env["adapter"]["adapter"] = adapter
        task = env["pipeline"].enqueue_parse(owner_user_id=owner, material_id=mid)
        result = env["pipeline"].run_parse(task["task_id"])
        assert result["status"] == "succeeded"
        assert adapter.submit_calls == []  # 可信文字不重复 OCR
        # 页面 success + 文本入索引
        from sqlalchemy import select

        from backend.materials.models import MaterialPageRow

        with env["db"].session() as sess:
            page = sess.execute(
                select(MaterialPageRow).where(
                    MaterialPageRow.material_id == mid, MaterialPageRow.page_no == 1)
            ).scalars().first()
            assert page is not None and page.status == "success"
        hits = env["index"].search(owner_user_id=owner, query="transaction isolation", material_ids=[mid])
        assert hits, "可信文本应进入检索索引"

    def test_text_material_single_page(self, env) -> None:
        owner = f"user_{uuid.uuid4().hex[:8]}"
        mid = _make_material(env, owner, "神经网络 卷积 conv2d_block_7".encode("utf-8"), kind="txt")
        env["adapter"]["adapter"] = SyntheticOcrAdapter()
        task = env["pipeline"].enqueue_parse(owner_user_id=owner, material_id=mid)
        result = env["pipeline"].run_parse(task["task_id"])
        assert result["status"] == "succeeded"
        hits = env["index"].search(owner_user_id=owner, query="神经网络", material_ids=[mid])
        assert hits

    def test_rerun_does_not_duplicate_chunks(self, env) -> None:
        owner = f"user_{uuid.uuid4().hex[:8]}"
        mid = _make_material(env, owner, _pdf(pages=1, with_text=True))
        env["adapter"]["adapter"] = SyntheticOcrAdapter()
        task = env["pipeline"].enqueue_parse(owner_user_id=owner, material_id=mid)
        env["pipeline"].run_parse(task["task_id"])
        from sqlalchemy import func, select

        from backend.materials.models import TextChunkRow

        with env["db"].session() as sess:
            count1 = sess.execute(
                select(func.count()).select_from(TextChunkRow).where(TextChunkRow.material_id == mid)
            ).scalar_one()
        task2 = env["pipeline"].enqueue_parse(owner_user_id=owner, material_id=mid)
        assert task2["task_id"] != task["task_id"]  # 终态后重试 → 新任务
        env["pipeline"].run_parse(task2["task_id"])
        with env["db"].session() as sess:
            count2 = sess.execute(
                select(func.count()).select_from(TextChunkRow).where(TextChunkRow.material_id == mid)
            ).scalar_one()
        assert count1 == count2


class TestOcrPath:
    def test_scanned_page_submits_and_fetches(self, env) -> None:
        owner = f"user_{uuid.uuid4().hex[:8]}"
        mid = _make_material(env, owner, _pdf(pages=1, with_text=False))  # 无文本层
        adapter = SyntheticOcrAdapter()
        env["adapter"]["adapter"] = adapter
        task = env["pipeline"].enqueue_parse(owner_user_id=owner, material_id=mid)
        result = env["pipeline"].run_parse(task["task_id"])
        assert result["status"] == "succeeded"
        assert adapter.submit_calls == [(mid, 1)]
        # 页面记录外部 job，等待 fetch
        from sqlalchemy import select

        from backend.materials.models import MaterialPageRow

        with env["db"].session() as sess:
            page = sess.execute(
                select(MaterialPageRow).where(
                    MaterialPageRow.material_id == mid, MaterialPageRow.page_no == 1)
            ).scalars().first()
            assert page is not None and page.ocr_job_id
        # fetch 任务落库并被执行
        fetch_tasks = env["tasks"].get_task(result.get("task_id") or "") if False else None
        # 直接构造 fetch：从事件/任务表找 ocr_result_fetch 任务
        with env["db"].session() as sess:
            from backend.tasks.service import LearningTaskRow

            fetch_row = sess.execute(
                select(LearningTaskRow).where(
                    LearningTaskRow.kind == "ocr_result_fetch",
                    LearningTaskRow.subject_id == f"{mid}:1",
                )
            ).scalars().first()
            assert fetch_row is not None
            fetch_id = fetch_row.task_id
        fetch_result = env["pipeline"].run_fetch(fetch_id)
        assert fetch_result["status"] == "succeeded"
        assert fetch_result["regions"] == 2
        # 文本区域进索引；结构图区域 partial（无文本）
        hits = env["index"].search(owner_user_id=owner, query="扫描页", material_ids=[mid])
        assert hits
        with env["db"].session() as sess:
            page = sess.execute(
                select(MaterialPageRow).where(
                    MaterialPageRow.material_id == mid, MaterialPageRow.page_no == 1)
            ).scalars().first()
            assert page.status == "partial"  # 结构图无文本 → 最多 partial

    def test_ocr_submit_failure_marks_page_failed(self, env) -> None:
        owner = f"user_{uuid.uuid4().hex[:8]}"
        mid = _make_material(env, owner, _pdf(pages=1, with_text=False))
        env["adapter"]["adapter"] = SyntheticOcrAdapter(fail_submit=True)
        task = env["pipeline"].enqueue_parse(owner_user_id=owner, material_id=mid)
        result = env["pipeline"].run_parse(task["task_id"])
        assert result["status"] == "succeeded"  # 页失败不算任务失败
        from sqlalchemy import select

        from backend.materials.models import MaterialPageRow

        with env["db"].session() as sess:
            page = sess.execute(
                select(MaterialPageRow).where(
                    MaterialPageRow.material_id == mid, MaterialPageRow.page_no == 1)
            ).scalars().first()
            assert page.status == "failed"
            assert page.failure_reason == "timeout"
        # 失败页重试：重置后再次入队可提交
        page.status = "pending"
        page.failure_reason = None
        sess.commit()
        env["adapter"]["adapter"] = SyntheticOcrAdapter()
        task2 = env["pipeline"].enqueue_parse(owner_user_id=owner, material_id=mid)
        result2 = env["pipeline"].run_parse(task2["task_id"])
        assert result2["status"] == "succeeded"
        assert env["adapter"]["adapter"].submit_calls == [(mid, 1)]


class TestCoordinateMapping:
    @pytest.mark.parametrize("rotation,expected", [
        (90, {"left": 10.0, "top": 240.0, "right": 80.0, "bottom": 290.0}),
        (180, {"left": 140.0, "top": 220.0, "right": 190.0, "bottom": 290.0}),
        (270, {"left": 120.0, "top": 10.0, "right": 190.0, "bottom": 60.0}),
        (0, {"left": 10.0, "top": 10.0, "right": 60.0, "bottom": 80.0}),
    ])
    def test_rotation_roundtrip(self, rotation: int, expected: dict) -> None:
        bbox = {"left": 10, "top": 10, "right": 60, "bottom": 80}
        got = map_bbox_to_pixels(bbox=bbox, page_width=200, page_height=300,
                                 rotation=rotation, coordinate_system="top_left")
        assert got == expected

    def test_bottom_left_flip(self) -> None:
        got = map_bbox_to_pixels(bbox={"left": 10, "top": 220, "right": 60, "bottom": 290},
                                 page_width=200, page_height=300, rotation=0,
                                 coordinate_system="bottom_left")
        assert got == {"left": 10.0, "top": 10.0, "right": 60.0, "bottom": 80.0}

    def test_normalize_ocr_regions(self) -> None:
        regions = normalize_ocr_regions({
            "layoutParsingResults": [{"prunedResult": {"parsing_res_list": [
                {"block_bbox": [1, 2, 3, 4], "block_label": "text", "block_content": "hi", "block_index": 0},
            ]}}],
        })
        assert len(regions) == 1
        assert regions[0]["kind"] == "text" and regions[0]["text"] == "hi"


class TestCrop:
    def _parsed_scanned_material(self, env, owner: str) -> str:
        from sqlalchemy import select

        mid = _make_material(env, owner, _pdf(pages=1, with_text=False))
        env["adapter"]["adapter"] = SyntheticOcrAdapter()
        task = env["pipeline"].enqueue_parse(owner_user_id=owner, material_id=mid)
        env["pipeline"].run_parse(task["task_id"])
        fetch_id = None
        with env["db"].session() as sess:
            from backend.tasks.service import LearningTaskRow

            row = sess.execute(
                select(LearningTaskRow).where(
                    LearningTaskRow.kind == "ocr_result_fetch",
                    LearningTaskRow.subject_id == f"{mid}:1",
                )
            ).scalars().first()
            fetch_id = row.task_id
        env["pipeline"].run_fetch(fetch_id)
        return mid

    def test_crop_text_region_creates_figure(self, env) -> None:
        owner = f"user_{uuid.uuid4().hex[:8]}"
        mid = self._parsed_scanned_material(env, owner)
        from sqlalchemy import select

        from backend.materials.models import MaterialRegionRow

        with env["db"].session() as sess:
            regions = sess.execute(
                select(MaterialRegionRow).where(
                    MaterialRegionRow.material_id == mid,
                    MaterialRegionRow.kind == "text",
                    MaterialRegionRow.status == "success",
                )
            ).scalars().all()
        assert regions
        crop = CropService(env["db"], storage=env["storage"])
        result = crop.crop_region(owner_user_id=owner, region_id=regions[0].region_id)
        assert result["figure_id"]
        payload = env["storage"].get(result["storage_key"])
        assert payload[:8] == b"\x89PNG\r\n\x1a\n"
        # inverse_transform 可复现（含页宽高与 bbox）
        import json

        with env["db"].session() as sess:
            from backend.materials.models import FigureAssetRow

            fig = sess.get(FigureAssetRow, result["figure_id"])
            assert fig is not None
            inv = json.loads(fig.inverse_transform)
        assert inv["page_width"] > 0 and "bbox" in inv

    def test_cross_account_crop_denied(self, env) -> None:
        owner = f"user_{uuid.uuid4().hex[:8]}"
        mid = self._parsed_scanned_material(env, owner)
        from sqlalchemy import select

        from backend.materials.models import MaterialRegionRow

        with env["db"].session() as sess:
            region = sess.execute(
                select(MaterialRegionRow).where(MaterialRegionRow.material_id == mid)
            ).scalars().first()
        crop = CropService(env["db"], storage=env["storage"])
        import pytest as _pytest

        from backend.materials.crop import CropError

        with _pytest.raises(CropError):
            crop.crop_region(owner_user_id="user_other", region_id=region.region_id)

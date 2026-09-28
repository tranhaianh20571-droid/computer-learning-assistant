"""镜像树一致性守护（通用规范 testing-spec §2 / 重构方案 O-D）。

镜像（tests/support/backend）与生产（server/backend/app）必须逐字节一致；
例外以显式白名单登记（垫片与测试专用迁移夹具，见
tests/support/backend/README.md）。任何未登记的差异或镜像独有文件都会
使本测试失败——这是防止"改了生产忘改镜像、测试静默跑旧代码"的机械闸门。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROD = ROOT / "server" / "backend" / "app"
MIRROR = ROOT / "tests" / "support" / "backend"

# 有意 divergence 白名单（内容不比较，仅要求存在）
ALLOWED_MISMATCH = {
    "__init__.py",  # 包级垫片：额外导出 Database/get_db/set_db
}

# 测试专用夹具：仅存于镜像，生产迁移链已被后续版本取代（ac03 使用）
MIRROR_ONLY = {
    "alembic/versions/11fd41567866_task_events_audit_logs_foundation.py",
    "alembic/versions/e3d25b84ccbb_slice0_identity_tasks_prompts.py",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_mirror_files_match_production():
    checked = 0
    errors: list[str] = []
    for f in sorted(MIRROR.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        rel = f.relative_to(MIRROR).as_posix()
        if rel in MIRROR_ONLY:
            continue
        if rel in ALLOWED_MISMATCH:
            checked += 1
            continue
        prod_file = PROD / rel
        if not prod_file.exists():
            errors.append(f"镜像独有文件未登记: {rel}")
            continue
        if _sha(f) != _sha(prod_file):
            errors.append(f"镜像与生产不一致: {rel}")
        checked += 1
    assert not errors, (
        "\n".join(errors)
        + f"\n（共检查 {checked} 个文件；同步方式见 tests/support/backend/README.md，"
          "新增例外必须在白名单登记）"
    )
    assert checked > 50, "镜像树异常缩小，检查 tests/support/backend 完整性"


def test_registered_exceptions_still_exist():
    for rel in sorted(MIRROR_ONLY | ALLOWED_MISMATCH):
        assert (MIRROR / rel).exists(), f"白名单条目已不存在，请更新登记: {rel}"

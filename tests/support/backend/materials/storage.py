"""受控文件存储（切片 2 T03/T04）。

- 原 PDF 与裁切图块存放在受控目录（MATERIALS_STORAGE_DIR，默认 server/storage），
  对象键不含用户可读文件名；浏览器只能经短时受权接口读对象。
- 写入最小权限；删除按对象键执行（真实卷清理归切片 5 调度）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


class StorageError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def default_base_dir() -> Path:
    env = os.environ.get("MATERIALS_STORAGE_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "storage"


class MaterialStorage:
    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else default_base_dir()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # 一次性解析（含符号链接/短路径），之后所有比较都用同一实例
        self._resolved_base = self.base_dir.resolve()

    def _resolve(self, key: str) -> Path:
        if not key or ".." in key or key.startswith("/"):
            raise StorageError("invalid_object_key", "object key rejected")
        path = (self.base_dir / key).resolve()
        # Windows 大小写不敏感：is_relative_to 在 WindowsPath 上折叠大小写
        if not path.is_relative_to(self._resolved_base):
            raise StorageError("invalid_object_key", "object key escapes base dir")
        return path

    def put(self, key: str, payload: bytes) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(payload)
        os.replace(tmp, path)
        return key

    def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.exists():
            raise StorageError("object_not_found", f"missing {key.split('/')[0]} object")
        return path.read_bytes()

    def delete(self, key: str) -> bool:
        path = self._resolve(key)
        if path.exists():
            path.unlink()
            return True
        return False

    # ---- 便捷键生成（不含用户内容） ----
    @staticmethod
    def original_key(material_id: str) -> str:
        return f"orig/{material_id}.bin"

    @staticmethod
    def figure_key(material_id: str, figure_id: str) -> str:
        return f"fig/{material_id}/{figure_id}.png"


__all__ = ["MaterialStorage", "StorageError", "default_base_dir"]

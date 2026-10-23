"""受控文件存储（切片 2 T03/T04）。

- 原 PDF 与裁切图块存放在受控目录（MATERIALS_STORAGE_DIR，默认 server/storage），
  对象键不含用户可读文件名；浏览器只能经短时受权接口读对象。
- 写入最小权限；删除按对象键执行（真实卷清理归切片 5 调度）。
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from .location import guarded, location_for


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
        self.location = location_for(Path(base_dir) if base_dir else default_base_dir())
        self.base_dir.mkdir(parents=True, exist_ok=True)

    @property
    def base_dir(self):
        return self.location.root()

    @property
    def _resolved_base(self):
        return self.base_dir.resolve()

    def _resolve(self, key: str) -> Path:
        if not key or ".." in key or key.startswith("/"):
            raise StorageError("invalid_object_key", "object key rejected")
        path = (self.base_dir / key).resolve()
        # Windows 大小写不敏感：is_relative_to 在 WindowsPath 上折叠大小写
        if not path.is_relative_to(self._resolved_base):
            raise StorageError("invalid_object_key", "object key escapes base dir")
        return path

    @guarded
    def put(self, key: str, payload: bytes) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(payload)
        os.replace(tmp, path)
        return key

    @guarded
    def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.exists():
            raise StorageError("object_not_found", f"missing {key.split('/')[0]} object")
        return path.read_bytes()

    @guarded
    def local_path(self, key: str) -> Path:
        path = self._resolve(key)
        if not path.is_file():
            raise StorageError("object_not_found")
        return path

    @guarded
    def put_file(self, key: str, source: Path) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            with source.open("rb") as incoming, tmp.open("wb") as output:
                shutil.copyfileobj(incoming, output, length=1024 * 1024)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return key

    @guarded
    def delete(self, key: str) -> bool:
        path = self._resolve(key)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    @contextmanager
    def atomic_output(self, key: str):
        """Publish a streamed object only after the writer closes successfully."""
        with self.location.lock:
            path = self._resolve(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(suffix=".part", dir=path.parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    yield output
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        if self.location.observer:
            self.location.observer(self, key, False)

    def iter_files(self, prefix: str = "", *, excluded_dirs=(), reject_links=False):
        with self.location.lock:
            yield from self._iter_files(prefix, excluded_dirs=excluded_dirs, reject_links=reject_links)

    def _iter_files(self, prefix: str = "", *, excluded_dirs=(), reject_links=False):
        """Walk ordinary files without traversing links or directory junctions."""
        root = self.base_dir / prefix.rstrip("/") if prefix else self.base_dir
        # Validate lexical components before resolve() can conceal a link.
        relative = root.relative_to(self.base_dir)
        current = self.base_dir
        for part in relative.parts:
            current = current / part
            if current.is_symlink() or current.is_junction():
                raise StorageError("invalid_object_key")
        self._resolve(prefix.rstrip("/") or ".")
        try:
            mode = root.stat().st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(mode):
            raise StorageError("invalid_object_key")
        def walk_error(exc):
            raise exc
        for directory, subdirs, names in os.walk(root, followlinks=False, onerror=walk_error):
            base = Path(directory)
            if reject_links and any(
                    (base / name).is_symlink() or (base / name).is_junction()
                    for name in [*subdirs, *names]):
                raise StorageError("invalid_object_key")
            subdirs[:] = sorted(
                name for name in subdirs if name not in excluded_dirs
                and not (base / name).is_symlink() and not (base / name).is_junction()
            )
            for name in sorted(names):
                path = base / name
                if path.is_symlink() or path.is_junction():
                    continue
                if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                    continue
                key = path.relative_to(self.base_dir).as_posix()
                if self._resolve(key) != path.resolve():
                    raise StorageError("invalid_object_key")
                yield key, path

    def delete_source_assets(self, material_id: str) -> None:
        """Delete only bounded, server-generated source objects."""
        directory = self._resolve(f"source-assets/{material_id}")
        if directory.is_dir():
            for asset in directory.iterdir():
                if asset.is_file():
                    self.delete(f"source-assets/{material_id}/{asset.name}")
            directory.rmdir()
        for suffix in (".json", "-visual.json"):
            self.delete(f"source-checkpoints/{material_id}{suffix}")

    # ---- 便捷键生成（不含用户内容） ----
    @staticmethod
    def original_key(material_id: str) -> str:
        return f"orig/{material_id}.bin"

    @staticmethod
    def figure_key(material_id: str, figure_id: str) -> str:
        return f"fig/{material_id}/{figure_id}.png"


__all__ = ["MaterialStorage", "StorageError", "default_base_dir"]

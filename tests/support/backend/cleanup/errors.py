"""切片 5 清理/恢复模块受控错误。"""

from __future__ import annotations


class CleanupError(Exception):
    def __init__(self, code: str, message: str = "", status: int | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        if status is not None:
            self.status = status


def cerr(code: str, message: str = "", status: int | None = None) -> CleanupError:
    return CleanupError(code, message, status)

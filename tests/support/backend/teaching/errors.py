"""切片 4 教学模块受控错误。"""

from __future__ import annotations


class TeachingError(Exception):
    def __init__(self, code: str, message: str = "", status: int | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        if status is not None:
            self.status = status


def terr(code: str, message: str = "", status: int | None = None) -> TeachingError:
    return TeachingError(code, message, status)

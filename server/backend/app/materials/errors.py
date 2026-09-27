"""切片 2 受控错误码与异常。

供应商原始异常只进入受控映射，不直接展示或写入日志（架构 §3.1、§6）。
错误码集合对应交接文档 §3.1/T02 第 3 步。
"""

from __future__ import annotations

from typing import Optional


class MaterialsError(Exception):
    """切片 2 领域异常：带受控错误码与 HTTP 状态。"""

    def __init__(self, code: str, message: str = "", status: int = 400) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message or code
        self.status = status


# 交接文档 §3.1 冻结的错误码
ERROR_CODES = {
    "material_not_found": 404,
    "invalid_file_signature": 400,
    "invalid_encoding": 400,
    "too_many_pages": 400,
    "file_too_large": 413,
    "too_many_files": 400,
    "quota_exceeded": 409,
    "material_already_deleted": 410,
    "index_out_of_range": 400,
    "page_state_conflict": 409,
    "unsupported_media_type": 415,
    "empty_file": 400,
    "pdf_encrypted": 400,
    "pdf_corrupt": 400,
    "render_limit_exceeded": 422,
    "region_not_found": 404,
    "figure_not_found": 404,
    "invalid_field": 400,
    "access_denied": 403,
}


def error(code: str, message: str = "") -> MaterialsError:
    return MaterialsError(code, message, ERROR_CODES.get(code, 400))


class RenderError(Exception):
    """受控渲染错误；不携带 PDF 原始正文。"""

    def __init__(self, code: str, detail: Optional[str] = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail

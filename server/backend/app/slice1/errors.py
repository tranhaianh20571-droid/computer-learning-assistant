"""切片 1 受控错误码与异常。

供应商原始异常只进入受控映射，不直接展示或写入日志（架构 §3.1、§6）。
"""

from __future__ import annotations

from typing import Optional


class Slice1Error(Exception):
    """切片 1 领域异常：带受控错误码与 HTTP 状态。"""

    def __init__(self, code: str, message: str = "", status: int = 400) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message or code
        self.status = status


# 交接文档 §3.1 冻结的错误码
ERROR_CODES = {
    "config_not_found": 404,
    "credential_invalid": 400,
    "capability_test_failed": 502,
    "connector_offline": 409,
    "nonce_replay": 409,
    "disclosure_expired": 409,
    "disclosure_revoked": 409,
    "disclosure_required": 403,
    "connector_revoked": 403,
    "request_expired": 409,
    "invalid_target": 400,
    "admin_only": 403,
    "protocol_mismatch": 400,
    "pairing_code_invalid": 400,
    "binding_not_found": 404,
    "capability_unavailable": 409,
    "invalid_field": 400,
    "access_denied": 403,
}


def error(code: str, message: str = "") -> Slice1Error:
    return Slice1Error(code, message, ERROR_CODES.get(code, 400))


def capability_unavailable(capability: str, detail: str = "") -> Slice1Error:
    msg = f"model_capability_unavailable: {capability}"
    if detail:
        msg = f"{msg} ({detail})"
    return Slice1Error("capability_unavailable", msg, 409)


def as_detail(exc: Slice1Error) -> dict:
    return {"error_code": exc.code, "message": exc.message}


class AdapterError(Exception):
    """外部适配器受控错误；不携带原始供应商正文。"""

    def __init__(self, code: str, status: int = 502, detail: Optional[str] = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = detail

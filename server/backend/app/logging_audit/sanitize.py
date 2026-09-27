"""T01：字段脱敏、控制字符清理、长度限制与供应商错误映射。

规范基线：文档/规范/logging-and-audit-spec.md 第 6 节。
所有不可信字符串先限长、移除控制字符并由 JSON 编码器输出。
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Mapping, MutableMapping, Optional

from .contracts import (
    DEFAULT_FIELD_MAX_LENGTH,
    FIELD_MAX_LENGTH,
    FORBIDDEN_FIELD_NAMES,
    MAX_TEMPLATE_PARAMS,
    VENDOR_ERROR_MAP,
    ContractError,
    validate_field_name,
)

# 控制字符（含零宽、BOM、换行注入）—— 一律移除
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\ufeff]")

# 敏感值模式（双重保险：即使字段名合法，值里也不允许出现）
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_API_KEY_RE = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9\-_]{8,}|api[_-]?key\s*[=:]\s*\S+|bearer\s+[A-Za-z0-9._\-]{8,}|"
    r"AKIA[0-9A-Z]{8,}|ghp_[A-Za-z0-9]{8,}|xox[baprs]-[A-Za-z0-9-]{8,})"
)
_URL_WITH_QUERY_RE = re.compile(r"https?://\S+\?\S+")
_ABS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\[^\s\"']+|/(?:home|Users|var|etc|tmp)/[^\s\"']+")
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")

# 结构化 ID 字段：只做控制字符清理与限长，不做敏感值遮蔽
# （trace_id/span_id/task_id 等本身就是 32/16 位十六进制，不能被长令牌规则误伤）
_ID_FIELDS = frozenset(
    {
        "trace_id",
        "span_id",
        "request_id",
        "task_id",
        "attempt_id",
        "event_id",
        "actor_id",
        "owner_user_id",
        "resource_id",
        "subject_id",
        "object_id",
        "target_actor_id",
        "admin_id",
        "config_id",
        "tombstone_id",
        "query_id",
        "external_request_id",
        "region_id",
    }
)

# 自由文本字段：完整脱敏
_TEXT_FIELDS = frozenset({"message", "reason", "template_params"})

REDACTED = "[REDACTED]"


def strip_control_chars(value: str) -> str:
    """移除控制字符与零宽字符，防止日志注入伪造新行。"""
    cleaned = _CONTROL_CHARS.sub("", value)
    # 规范化换行为空格，避免多行 JSON
    cleaned = cleaned.replace("\n", " ").replace("\r", " ")
    return cleaned


def limit_length(value: str, field_name: str) -> str:
    max_len = FIELD_MAX_LENGTH.get(field_name, DEFAULT_FIELD_MAX_LENGTH)
    if len(value) <= max_len:
        return value
    return value[: max_len - 1] + "…"


def redact_sensitive_text(value: str) -> str:
    """遮蔽值层面的邮箱、密钥、带查询 URL、绝对路径和长令牌。"""
    value = _EMAIL_RE.sub(REDACTED, value)
    value = _API_KEY_RE.sub(REDACTED, value)
    value = _URL_WITH_QUERY_RE.sub(REDACTED, value)
    value = _ABS_PATH_RE.sub(REDACTED, value)
    value = _LONG_TOKEN_RE.sub(REDACTED, value)
    return value


def sanitize_string(value: str, field_name: str) -> str:
    value = strip_control_chars(value)
    if field_name in _ID_FIELDS:
        # ID 字段只限长，不做敏感值遮蔽（避免误伤合法 trace/task ID）
        return limit_length(value, field_name)
    value = redact_sensitive_text(value)
    value = limit_length(value, field_name)
    return value


def sanitize_template_params(params: Any) -> dict:
    """模板参数仅允许有限深度的 JSON 可序列化标量/列表/字典，且值同样脱敏。"""
    if not isinstance(params, Mapping):
        raise ContractError("INVALID_TEMPLATE_PARAMS", "template_params 必须是对象")
    if len(params) > MAX_TEMPLATE_PARAMS:
        raise ContractError("INVALID_TEMPLATE_PARAMS", "template_params 数量超限")
    out: dict[str, Any] = {}
    for key, val in params.items():
        if not isinstance(key, str) or not key.isidentifier() and not re.match(r"^[a-z0-9_]+$", key):
            raise ContractError("INVALID_TEMPLATE_PARAMS", f"非法模板参数键: {key!r}")
        out[key] = _sanitize_template_value(val, depth=0)
    return out


def _sanitize_template_value(val: Any, depth: int) -> Any:
    if depth > 3:
        raise ContractError("INVALID_TEMPLATE_PARAMS", "template_params 嵌套过深")
    if val is None or isinstance(val, bool):
        return val
    if isinstance(val, int) and not isinstance(val, bool):
        return val
    if isinstance(val, float):
        return val
    if isinstance(val, str):
        return sanitize_string(val, "template_params")
    if isinstance(val, (list, tuple)):
        return [_sanitize_template_value(v, depth + 1) for v in val[:20]]
    if isinstance(val, Mapping):
        return {
            str(k)[:40]: _sanitize_template_value(v, depth + 1)
            for k, v in list(val.items())[:20]
        }
    raise ContractError("INVALID_TEMPLATE_PARAMS", f"不支持的模板参数类型: {type(val).__name__}")


def map_vendor_error(raw: Any) -> str:
    """供应商原始错误只进入受控映射，不直接显示或写入日志。"""
    text = str(raw).lower() if raw is not None else ""
    for needle, code in VENDOR_ERROR_MAP.items():
        if needle in text:
            return code
    return "INTERNAL_ERROR"


def sanitize_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """按允许名单过滤并脱敏一条日志/事件/审计记录。

    - 未知字段或禁止字段名 → 拒绝
    - 字符串值：控制字符清理、敏感值遮蔽、限长
    - duration_ms 必须为非负整数
    - 禁止字段名的值即使存在也会被拒绝，不做静默丢弃（fail-closed）
    """
    out: dict[str, Any] = {}
    for key, value in record.items():
        validate_field_name(key)
        if key in FORBIDDEN_FIELD_NAMES:
            raise ContractError("FORBIDDEN_FIELD", f"禁止字段: {key}")
        out[key] = _sanitize_value(key, value)
    return out


def _sanitize_value(key: str, value: Any) -> Any:
    if value is None:
        return None
    if key == "template_params":
        return sanitize_template_params(value)
    if key == "duration_ms":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError("INVALID_FIELD", "duration_ms 必须是整数")
        if value < 0:
            raise ContractError("INVALID_FIELD", "duration_ms 不能为负")
        return value
    if key in {"progress_current", "progress_total", "sequence", "attempt", "page_no", "page_count", "last_sequence", "queue_length", "dropped_count", "pending_count", "cleaned_count", "schema_version", "http_status"}:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ContractError("INVALID_FIELD", f"{key} 必须是整数")
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, str):
        return sanitize_string(value, key)
    if isinstance(value, (list, tuple)):
        return [_sanitize_value(key, v) for v in value[:50]]
    raise ContractError("INVALID_FIELD", f"{key} 不支持类型 {type(value).__name__}")


def dumps_line(record: Mapping[str, Any]) -> str:
    """序列化为一行 UTF-8 JSON（运行日志格式）。"""
    sanitized = sanitize_record(record)
    return json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"), allow_nan=False)

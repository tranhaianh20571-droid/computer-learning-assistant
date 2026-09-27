"""T01：日志字段允许名单、事件注册表、错误码与阶段枚举。

规范基线：文档/规范/logging-and-audit-spec.md 第 2、3、6 节。
生产日志采用字段允许名单；未知字段默认拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, FrozenSet, Mapping, Optional, Tuple

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"


class LogEventType(str, Enum):
    """事件数据类型：运行日志 / 任务事件 / 审计（可组合）。"""

    RUNTIME = "runtime"
    TASK = "task"
    AUDIT = "audit"
    METRIC = "metric"


class TaskStage(str, Enum):
    UPLOAD = "upload"
    PARSE = "parse"
    OCR = "ocr"
    FIGURE_CROP = "figure_crop"
    INDEX = "index"
    RETRIEVE = "retrieve"
    SEARCH = "search"
    GENERATE = "generate"
    BOARD = "board"
    TTS = "tts"
    SAVE = "save"
    CLEANUP = "cleanup"


class TaskStatus(str, Enum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    CANCELLED = "cancelled"
    STALE_DISCARDED = "stale_discarded"


class AlertLevel(str, Enum):
    NONE = "none"
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# 公共字段允许名单
# ---------------------------------------------------------------------------

# 必填公共字段
REQUIRED_COMMON_FIELDS: FrozenSet[str] = frozenset(
    {"schema_version", "timestamp", "level", "event", "service", "environment"}
)

# 允许出现在结构化日志/任务事件/审计中的全部字段（允许名单）
# 禁止字段（正文、密钥、邮箱、完整 URL 等）一律不在其中，构造时也会被 sanitize 拒绝。
ALLOWED_FIELDS: FrozenSet[str] = frozenset(
    {
        # 公共
        "schema_version",
        "timestamp",
        "level",
        "event",
        "service",
        "environment",
        "message",
        # 关联
        "trace_id",
        "span_id",
        "request_id",
        "task_id",
        "attempt",
        "attempt_id",
        "generation",
        "revision",
        "sequence",
        "event_id",
        # 主体与资源（不透明 ID）
        "actor_id",
        "owner_user_id",
        "resource_type",
        "resource_id",
        "subject_id",
        "object_type",
        "object_id",
        "target_actor_id",
        "admin_id",
        # 阶段与结果
        "stage",
        "status",
        "error_code",
        "duration_ms",
        "result",
        "reason",
        "retryable",
        # 任务事件展示
        "progress_current",
        "progress_total",
        "template_id",
        "template_params",
        # 配置与外部能力
        "config_id",
        "config_version",
        "capability",
        "exporter",
        "queue_length",
        "external_request_id",
        # 分页/区域/生成
        "page_no",
        "page_count",
        "region_id",
        # 审计
        "audit_action",
        "query_id",
        "role",
        # 观测降级
        "degraded",
        "dropped_count",
        # 事件流
        "last_sequence",
        "cursor",
        "http_status",
        # 清理
        "tombstone_id",
        "cleanup_stage",
        "pending_count",
        "cleaned_count",
    }
)

# 明确禁止记录的字段名（双重保险：不在允许名单 + 命中即拒绝）
FORBIDDEN_FIELD_NAMES: FrozenSet[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "refresh_token",
        "cookie",
        "authorization",
        "session_id",
        "email",
        "mail",
        "filename",
        "file_name",
        "filepath",
        "file_path",
        "url",
        "query",
        "prompt",
        "response",
        "request_body",
        "response_body",
        "content",
        "text",
        "ocr_text",
        "transcript",
        "audio",
        "image",
        "payload",
        "body",
        "raw_error",
        "stacktrace",
        "stack_trace",
        "exception",
        "dsn",
        "connection_string",
        "private_key",
        "master_key",
    }
)

# 每个字段的最大长度（超出截断；正文类字段本就不在允许名单）
FIELD_MAX_LENGTH: Mapping[str, int] = {
    "message": 200,
    "reason": 200,
    "template_id": 100,
    "error_code": 64,
    "event": 120,
    "service": 40,
    "environment": 40,
    "stage": 40,
    "status": 40,
    "capability": 64,
    "exporter": 40,
    "resource_type": 40,
    "object_type": 40,
    "audit_action": 64,
    "cleanup_stage": 40,
}

DEFAULT_FIELD_MAX_LENGTH = 120
MAX_TEMPLATE_PARAMS = 20


# ---------------------------------------------------------------------------
# 错误码注册表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorCodeSpec:
    code: str
    stage: TaskStage
    retryable: bool
    user_visible: bool
    alert_level: AlertLevel
    description: str


ERROR_CODES: Mapping[str, ErrorCodeSpec] = {
    spec.code: spec
    for spec in (
        ErrorCodeSpec(
            "UPLOAD_TYPE_REJECTED", TaskStage.UPLOAD, False, True, AlertLevel.NONE,
            "文件类型或签名校验失败",
        ),
        ErrorCodeSpec(
            "UPLOAD_QUOTA_EXCEEDED", TaskStage.UPLOAD, False, True, AlertLevel.NONE,
            "超出个人配额",
        ),
        ErrorCodeSpec(
            "PDF_ENCRYPTED", TaskStage.PARSE, False, True, AlertLevel.NONE,
            "PDF 加密无法解析",
        ),
        ErrorCodeSpec(
            "PDF_PAGE_RENDER_FAILED", TaskStage.PARSE, True, True, AlertLevel.WARNING,
            "单页渲染失败",
        ),
        ErrorCodeSpec(
            "OCR_PAGE_TIMEOUT", TaskStage.OCR, True, True, AlertLevel.WARNING,
            "单页 OCR 超时",
        ),
        ErrorCodeSpec(
            "OCR_OUTPUT_INVALID", TaskStage.OCR, True, True, AlertLevel.WARNING,
            "OCR 输出结构无效",
        ),
        ErrorCodeSpec(
            "FIGURE_CROP_FAILED", TaskStage.FIGURE_CROP, True, True, AlertLevel.WARNING,
            "图块裁切失败",
        ),
        ErrorCodeSpec(
            "RETRIEVAL_UNAVAILABLE", TaskStage.RETRIEVE, True, True, AlertLevel.WARNING,
            "检索索引不可用",
        ),
        ErrorCodeSpec(
            "SEARCH_TIMEOUT", TaskStage.SEARCH, True, True, AlertLevel.WARNING,
            "联网搜索超时",
        ),
        ErrorCodeSpec(
            "MODEL_NOT_CONFIGURED", TaskStage.GENERATE, False, True, AlertLevel.NONE,
            "模型未配置",
        ),
        ErrorCodeSpec(
            "MODEL_TIMEOUT", TaskStage.GENERATE, True, True, AlertLevel.WARNING,
            "模型调用超时",
        ),
        ErrorCodeSpec(
            "BOARD_SCHEMA_INVALID", TaskStage.BOARD, True, True, AlertLevel.WARNING,
            "板书 schema 校验失败",
        ),
        ErrorCodeSpec(
            "TTS_TIMEOUT", TaskStage.TTS, True, True, AlertLevel.WARNING,
            "TTS 生成超时",
        ),
        ErrorCodeSpec(
            "CHECKPOINT_SAVE_FAILED", TaskStage.SAVE, True, True, AlertLevel.CRITICAL,
            "位置保存失败",
        ),
        ErrorCodeSpec(
            "STALE_RESULT_DISCARDED", TaskStage.SAVE, False, False, AlertLevel.WARNING,
            "旧 generation/迟到结果被拒绝",
        ),
        ErrorCodeSpec(
            "CLEANUP_OVERDUE", TaskStage.CLEANUP, True, False, AlertLevel.CRITICAL,
            "在线清理逾期",
        ),
        ErrorCodeSpec(
            "TASK_LEASE_EXPIRED", TaskStage.SAVE, False, False, AlertLevel.WARNING,
            "任务租约过期，结果只能写诊断",
        ),
        ErrorCodeSpec(
            "EVENTS_EXPIRED", TaskStage.SAVE, False, True, AlertLevel.NONE,
            "SSE 事件游标过期，需 REST 全量重建",
        ),
        ErrorCodeSpec(
            "ACCESS_DENIED", TaskStage.SAVE, False, False, AlertLevel.WARNING,
            "跨账户或越权访问被拒绝",
        ),
        ErrorCodeSpec(
            "AUDIT_WRITE_FAILED", TaskStage.SAVE, False, False, AlertLevel.CRITICAL,
            "安全审计写入失败",
        ),
        ErrorCodeSpec(
            "OBSERVABILITY_EXPORT_FAILED", TaskStage.SAVE, False, False, AlertLevel.WARNING,
            "观测导出失败，业务继续",
        ),
        ErrorCodeSpec(
            "PROMPT_UNAVAILABLE", TaskStage.GENERATE, False, True, AlertLevel.WARNING,
            "提示词绑定不可用，禁止调用模型",
        ),
        ErrorCodeSpec(
            "INTERNAL_ERROR", TaskStage.SAVE, False, False, AlertLevel.CRITICAL,
            "内部错误，已映射为受控错误码",
        ),
    )
}

# 供应商原始错误 → 受控错误码（示例映射；不把原始异常写入日志）
VENDOR_ERROR_MAP: Mapping[str, str] = {
    "timeout": "MODEL_TIMEOUT",
    "connection_error": "RETRIEVAL_UNAVAILABLE",
    "rate_limit": "SEARCH_TIMEOUT",
    "invalid_api_key": "MODEL_NOT_CONFIGURED",
    "quota_exceeded": "UPLOAD_QUOTA_EXCEEDED",
    "encrypted_pdf": "PDF_ENCRYPTED",
    "output_schema_mismatch": "OCR_OUTPUT_INVALID",
}


# ---------------------------------------------------------------------------
# 事件注册表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EventSpec:
    name: str
    types: Tuple[LogEventType, ...]
    required_fields: FrozenSet[str]
    optional_fields: FrozenSet[str] = field(default_factory=frozenset)
    user_visible: bool = False
    retryable: bool = False
    alert_level: AlertLevel = AlertLevel.NONE
    description: str = ""


_COMMON_REQUIRED = REQUIRED_COMMON_FIELDS

EVENT_REGISTRY: Mapping[str, EventSpec] = {
    spec.name: spec
    for spec in (
        EventSpec(
            "task.accepted",
            (LogEventType.RUNTIME, LogEventType.TASK),
            _COMMON_REQUIRED | frozenset({"task_id", "trace_id", "attempt"}),
            frozenset({"request_id", "actor_id", "subject_id", "generation", "stage"}),
            user_visible=True,
            description="任务受理",
        ),
        EventSpec(
            "task.started",
            (LogEventType.RUNTIME, LogEventType.TASK),
            _COMMON_REQUIRED | frozenset({"task_id", "trace_id", "attempt", "stage"}),
            frozenset({"request_id", "actor_id", "subject_id", "generation"}),
            user_visible=True,
            description="任务阶段开始",
        ),
        EventSpec(
            "task.stage.succeeded",
            (LogEventType.RUNTIME, LogEventType.TASK),
            _COMMON_REQUIRED
            | frozenset({"task_id", "stage", "status", "duration_ms"}),
            frozenset(
                {
                    "trace_id",
                    "attempt",
                    "actor_id",
                    "progress_current",
                    "progress_total",
                    "page_no",
                    "page_count",
                }
            ),
            user_visible=True,
            description="阶段成功",
        ),
        EventSpec(
            "task.stage.partial",
            (LogEventType.RUNTIME, LogEventType.TASK),
            _COMMON_REQUIRED
            | frozenset({"task_id", "stage", "status", "duration_ms"}),
            frozenset({"trace_id", "attempt", "error_code", "page_no", "reason"}),
            user_visible=True,
            description="阶段部分可用",
        ),
        EventSpec(
            "task.stage.failed",
            (LogEventType.RUNTIME, LogEventType.TASK),
            _COMMON_REQUIRED
            | frozenset({"task_id", "stage", "status", "duration_ms", "error_code"}),
            frozenset({"trace_id", "attempt", "retryable", "page_no", "reason"}),
            user_visible=True,
            description="阶段失败",
        ),
        EventSpec(
            "task.retry_scheduled",
            (LogEventType.RUNTIME, LogEventType.TASK),
            _COMMON_REQUIRED | frozenset({"task_id", "attempt", "error_code"}),
            frozenset({"trace_id", "stage", "generation", "retryable"}),
            user_visible=True,
            description="已安排重试",
        ),
        EventSpec(
            "task.cancelled",
            (LogEventType.RUNTIME, LogEventType.TASK),
            _COMMON_REQUIRED | frozenset({"task_id", "status"}),
            frozenset({"trace_id", "attempt", "generation", "actor_id"}),
            user_visible=True,
            description="任务取消",
        ),
        EventSpec(
            "task.stale_discarded",
            (LogEventType.RUNTIME, LogEventType.TASK),
            _COMMON_REQUIRED | frozenset({"task_id", "attempt", "generation"}),
            frozenset({"trace_id", "error_code", "status"}),
            user_visible=True,
            description="过期结果仅显示状态，不覆盖当前结果",
        ),
        EventSpec(
            "task.events.expired",
            (LogEventType.RUNTIME,),
            _COMMON_REQUIRED | frozenset({"task_id", "last_sequence"}),
            frozenset({"actor_id", "request_id", "cursor"}),
            user_visible=False,
            description="SSE 游标过期，映射为客户端全量刷新",
        ),
        EventSpec(
            "auth.login.failed",
            (LogEventType.AUDIT,),
            _COMMON_REQUIRED | frozenset({"actor_id", "result", "trace_id"}),
            frozenset({"reason", "request_id"}),
            user_visible=True,
            description="登录失败（仅必要的个人历史）",
        ),
        EventSpec(
            "auth.register.submitted",
            (LogEventType.AUDIT,),
            _COMMON_REQUIRED | frozenset({"actor_id", "result"}),
            frozenset({"trace_id", "object_type", "object_id"}),
            user_visible=True,
            description="注册提交",
        ),
        EventSpec(
            "auth.email.verified",
            (LogEventType.AUDIT,),
            _COMMON_REQUIRED | frozenset({"actor_id", "result"}),
            frozenset({"trace_id", "object_type", "object_id"}),
            user_visible=True,
            description="邮箱验证成功",
        ),
        EventSpec(
            "auth.password.reset_requested",
            (LogEventType.AUDIT,),
            _COMMON_REQUIRED | frozenset({"actor_id", "result"}),
            frozenset({"trace_id", "object_type", "object_id"}),
            user_visible=True,
            description="申请密码重置",
        ),
        EventSpec(
            "auth.password.reset_completed",
            (LogEventType.AUDIT,),
            _COMMON_REQUIRED | frozenset({"actor_id", "result"}),
            frozenset({"trace_id", "object_type", "object_id"}),
            user_visible=True,
            description="密码重置完成",
        ),
        EventSpec(
            "auth.session.revoked",
            (LogEventType.AUDIT,),
            _COMMON_REQUIRED | frozenset({"actor_id", "result", "trace_id"}),
            frozenset({"target_actor_id", "reason"}),
            user_visible=True,
            description="会话撤销",
        ),
        EventSpec(
            "admin.approval.changed",
            (LogEventType.AUDIT,),
            _COMMON_REQUIRED
            | frozenset({"admin_id", "target_actor_id", "object_type", "object_id", "result"}),
            frozenset({"trace_id", "reason"}),
            user_visible=False,
            description="管理员审批变更",
        ),
        EventSpec(
            "config.changed",
            (LogEventType.AUDIT,),
            _COMMON_REQUIRED
            | frozenset({"actor_id", "object_type", "object_id", "result"}),
            frozenset({"config_id", "config_version", "capability", "trace_id"}),
            user_visible=False,
            description="能力配置变更",
        ),
        EventSpec(
            "external.disclosure.confirmed",
            (LogEventType.AUDIT,),
            _COMMON_REQUIRED
            | frozenset({"actor_id", "capability", "result"}),
            frozenset({"config_id", "object_id", "trace_id"}),
            user_visible=True,
            description="外发确认",
        ),
        EventSpec(
            "connector.bound",
            (LogEventType.AUDIT,),
            _COMMON_REQUIRED | frozenset({"actor_id", "object_id", "result"}),
            frozenset({"trace_id"}),
            user_visible=True,
            description="连接器绑定",
        ),
        EventSpec(
            "connector.revoked",
            (LogEventType.AUDIT,),
            _COMMON_REQUIRED | frozenset({"actor_id", "object_id", "result"}),
            frozenset({"trace_id", "reason"}),
            user_visible=True,
            description="连接器撤销",
        ),
        EventSpec(
            "access.denied",
            (LogEventType.RUNTIME, LogEventType.AUDIT),
            _COMMON_REQUIRED
            | frozenset({"actor_id", "resource_type", "resource_id", "result", "reason"}),
            frozenset({"trace_id", "request_id", "task_id"}),
            user_visible=False,
            description="越权访问拒绝",
        ),
        EventSpec(
            "resource.delete.requested",
            (LogEventType.AUDIT, LogEventType.RUNTIME),
            _COMMON_REQUIRED
            | frozenset({"actor_id", "object_type", "object_id", "result"}),
            frozenset({"tombstone_id", "trace_id", "cleanup_stage"}),
            user_visible=True,
            description="资源删除请求",
        ),
        EventSpec(
            "cleanup.completed",
            (LogEventType.AUDIT, LogEventType.RUNTIME),
            _COMMON_REQUIRED | frozenset({"cleanup_stage", "result"}),
            frozenset(
                {
                    "tombstone_id",
                    "object_type",
                    "object_id",
                    "pending_count",
                    "cleaned_count",
                    "duration_ms",
                    "trace_id",
                }
            ),
            user_visible=True,
            description="清理完成",
        ),
        EventSpec(
            "restore.locked",
            (LogEventType.AUDIT, LogEventType.RUNTIME),
            _COMMON_REQUIRED | frozenset({"result"}),
            frozenset({"tombstone_id", "reason", "trace_id"}),
            user_visible=True,
            description="备份恢复锁定",
        ),
        EventSpec(
            "audit.query.executed",
            (LogEventType.AUDIT,),
            _COMMON_REQUIRED
            | frozenset({"actor_id", "result", "query_id"}),
            frozenset({"role", "trace_id", "object_type"}),
            user_visible=False,
            description="管理员审计查询自身留痕",
        ),
        EventSpec(
            "observability.export.failed",
            (LogEventType.RUNTIME, LogEventType.METRIC),
            _COMMON_REQUIRED | frozenset({"exporter", "error_code"}),
            frozenset({"queue_length", "dropped_count", "degraded"}),
            user_visible=False,
            description="观测导出失败",
        ),
    )
}


# ---------------------------------------------------------------------------
# 校验函数
# ---------------------------------------------------------------------------


class ContractError(ValueError):
    """契约校验失败。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def is_allowed_field(name: str) -> bool:
    return name in ALLOWED_FIELDS and name not in FORBIDDEN_FIELD_NAMES


def validate_field_name(name: str) -> None:
    if name in FORBIDDEN_FIELD_NAMES:
        raise ContractError("FORBIDDEN_FIELD", f"禁止字段: {name}")
    if name not in ALLOWED_FIELDS:
        raise ContractError("UNKNOWN_FIELD", f"未知字段不在允许名单: {name}")


def validate_error_code(code: str) -> ErrorCodeSpec:
    if code not in ERROR_CODES:
        raise ContractError("UNKNOWN_ERROR_CODE", f"未注册错误码: {code}")
    return ERROR_CODES[code]


def validate_stage(stage: str) -> TaskStage:
    try:
        return TaskStage(stage)
    except ValueError as exc:
        raise ContractError("UNKNOWN_STAGE", f"未注册阶段: {stage}") from exc


def validate_status(status: str) -> TaskStatus:
    try:
        return TaskStatus(status)
    except ValueError as exc:
        raise ContractError("UNKNOWN_STATUS", f"未注册状态: {status}") from exc


def validate_level(level: str) -> LogLevel:
    try:
        return LogLevel(level)
    except ValueError as exc:
        raise ContractError("UNKNOWN_LEVEL", f"未注册级别: {level}") from exc


def validate_event_name(event: str) -> EventSpec:
    if event not in EVENT_REGISTRY:
        raise ContractError("UNKNOWN_EVENT", f"未注册事件: {event}")
    return EVENT_REGISTRY[event]


def build_registry_document() -> dict:
    """导出机器可读注册表（供 schemas/ 版本化副本使用）。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "required_common_fields": sorted(REQUIRED_COMMON_FIELDS),
        "allowed_fields": sorted(ALLOWED_FIELDS),
        "forbidden_field_names": sorted(FORBIDDEN_FIELD_NAMES),
        "field_max_length": dict(FIELD_MAX_LENGTH),
        "default_field_max_length": DEFAULT_FIELD_MAX_LENGTH,
        "stages": [s.value for s in TaskStage],
        "statuses": [s.value for s in TaskStatus],
        "levels": [lv.value for lv in LogLevel],
        "error_codes": {
            code: {
                "stage": spec.stage.value,
                "retryable": spec.retryable,
                "user_visible": spec.user_visible,
                "alert_level": spec.alert_level.value,
                "description": spec.description,
            }
            for code, spec in ERROR_CODES.items()
        },
        "vendor_error_map": dict(VENDOR_ERROR_MAP),
        "events": {
            name: {
                "types": [t.value for t in spec.types],
                "required_fields": sorted(spec.required_fields),
                "optional_fields": sorted(spec.optional_fields),
                "user_visible": spec.user_visible,
                "retryable": spec.retryable,
                "alert_level": spec.alert_level.value,
                "description": spec.description,
            }
            for name, spec in EVENT_REGISTRY.items()
        },
    }

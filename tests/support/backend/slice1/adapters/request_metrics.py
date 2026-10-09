"""One observation per HTTP request; never capture URLs or wire content."""
from time import monotonic
from ...logging_audit.logger import StructuredLogger

_logger = StructuredLogger(service="api")


def failure_code(exc):
    code = getattr(exc, "code", "")
    return {"timeout": "MODEL_TIMEOUT", "connection_error": "MODEL_CONNECTION_ERROR",
        "credential_invalid": "MODEL_CREDENTIAL_INVALID", "rate_limited": "MODEL_RATE_LIMITED",
        "response_too_large": "MODEL_RESPONSE_TOO_LARGE", "unsupported_capability": "MODEL_CAPABILITY_UNAVAILABLE",
        "capability_test_failed": "MODEL_PROVIDER_ERROR"}.get(code, "MODEL_PROVIDER_ERROR")


def observe(adapter, started, *, first_chunk_ms=None, exc=None):
    try:
        _logger.log("model.request.failed" if exc else "model.request.completed",
            level="WARN" if exc else "INFO", stage="generate", status="failed" if exc else "succeeded",
            duration_ms=max(0, int((monotonic()-started)*1000)),
            error_code=failure_code(exc) if exc else None,
            config_id=getattr(adapter, "config_id", None), config_version=getattr(adapter, "config_version", None),
            task_id=getattr(adapter, "task_id", None), first_chunk_ms=first_chunk_ms)
    except Exception:
        pass

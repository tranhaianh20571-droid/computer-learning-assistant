"""Block unstructured HTTP/model SDK records before any logging handler sees them.

SDK diagnostics may contain URLs, credentials, headers, bodies and exception
text at any level. Application diagnostics use StructuredLogger instead. A
parent logger filter/level does not protect child loggers (or their own handlers),
so enforce this narrow namespace policy at Logger.handle, before propagation,
formatting, logging capture and error-reporting handlers.
"""
from __future__ import annotations

from functools import wraps
import logging
import threading


# httpx2 and google_genai are actual logger names used by installed SDKs;
# they are not descendants of httpx or google.genai respectively.
SDK_LOGGER_NAMESPACES = (
    "httpx", "httpx2", "httpcore", "httpcore2", "google_genai", "google",
    "pydantic_ai", "pydanticai", "openai", "anthropic", "requests", "urllib3",
    "aiohttp",
)
_INSTALL_LOCK = threading.Lock()
_GUARD_MARKER = "_learning_sdk_logging_safety"


def _is_sdk_logger(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".")
               for prefix in SDK_LOGGER_NAMESPACES)


def install_sdk_logging_safety() -> None:
    """Idempotently suppress raw SDK logs, including future child loggers.

    Do not globally disable logging or scrub arbitrary strings. Non-SDK records
    retain the existing dispatch implementation and application JSON streams
    remain independent. SDK log levels/handlers may change after installation
    without reopening this output path.
    """
    with _INSTALL_LOCK:
        previous_handle = logging.Logger.handle
        # app and the test mirror may both be loaded in one process.
        if getattr(previous_handle, _GUARD_MARKER, False):
            return

        @wraps(previous_handle)
        def safe_handle(self: logging.Logger, record: logging.LogRecord) -> None:
            if _is_sdk_logger(record.name):
                return
            previous_handle(self, record)

        setattr(safe_handle, _GUARD_MARKER, True)
        logging.Logger.handle = safe_handle

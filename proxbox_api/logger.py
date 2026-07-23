"""Logging setup utilities for console and file outputs."""

import contextvars
import logging
import os
import re
import sys
import time
import traceback
from collections.abc import Callable, Mapping
from functools import wraps
from logging.handlers import TimedRotatingFileHandler
from typing import ParamSpec, TypeVar

from fastapi import WebSocket

from proxbox_api.constants import DEFAULT_LOG_PATH

# Third-party loggers that are verbose at INFO but rarely operator-meaningful.
# Suppressed to WARNING by default; restored to DEBUG when PROXBOX_LOG_LEVEL=DEBUG.
_THIRD_PARTY_NOISY = [
    "netbox_sdk.client",
    "netbox_sdk.schema",
]

_VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@",
    re.IGNORECASE,
)
_QUERY_SECRET_RE = re.compile(
    r"(?P<prefix>[?&](?:api[_-]?key|(?:api|access)[_-]?token|client[_-]?secret|auth(?:entication|orization)?|cookie|credential|key|pass(?:phrase|word|wd)?|pwd|secret|token|(?:rgw[_-]?)?access[_-]?key|private[_-]?key)=)[^&\s]+",
    re.IGNORECASE,
)

_SENSITIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(X-Proxbox-API-Key\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(Authorization\s*[:=]\s*)"
            r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+(?:\s+[^\s,;}\]]+)?)",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"\bBearer\s+[^\s,;}\]]+", re.IGNORECASE), "Bearer [REDACTED]"),
    (
        re.compile(
            r"((?:[A-Za-z0-9_-]*(?:token|key|password|secret)[A-Za-z0-9_-]*)"
            r"\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(\"[^\"]*(?:token|key|password|secret)[^\"]*\"\s*:\s*)"
            r"(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)",
            re.IGNORECASE,
        ),
        r'\1"[REDACTED]"',
    ),
]

_SECRET_KEY_MARKERS = (
    "password",
    "passphrase",
    "passwd",
    "pwd",
    "secret",
    "token",
    "credential",
    "authorization",
    "cookie",
    "apikey",
    "accesskey",
    "privatekey",
    "xproxboxapikey",
)
_STANDARD_LOG_RECORD_ATTRIBUTES = frozenset(logging.makeLogRecord({}).__dict__) | {
    "asctime",
    "message",
}


def _redact(text: str) -> str:
    text = _URL_USERINFO_RE.sub(r"\g<scheme>[REDACTED]@", text)
    text = _QUERY_SECRET_RE.sub(r"\g<prefix>[REDACTED]", text)
    for pattern, replacement in _SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _normalized_log_key(value: object) -> str:
    """Normalize snake/camel/kebab/space-separated keys for secret matching."""

    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value))
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _is_sensitive_log_key(value: object) -> bool:
    normalized = _normalized_log_key(value)
    return any(marker in normalized for marker in _SECRET_KEY_MARKERS)


def _safe_log_value(value: object, *, _seen: set[int] | None = None) -> object:
    """Recursively normalize values before a handler or formatter sees them."""

    if isinstance(value, BaseException):
        return f"{type(value).__name__}: [REDACTED]"
    if isinstance(value, str):
        return _redact(value)
    if value is None or isinstance(value, bool | int | float):
        return value

    seen = _seen if _seen is not None else set()
    identity = id(value)
    if identity in seen:
        return "[REDACTED_RECURSION]"

    if isinstance(value, Mapping):
        seen.add(identity)
        try:
            return {
                key: (
                    "[REDACTED]"
                    if _is_sensitive_log_key(key)
                    else _safe_log_value(item, _seen=seen)
                )
                for key, item in value.items()
            }
        finally:
            seen.remove(identity)
    if isinstance(value, list | tuple | set | frozenset):
        seen.add(identity)
        try:
            safe_items = [_safe_log_value(item, _seen=seen) for item in value]
        finally:
            seen.remove(identity)
        return tuple(safe_items) if isinstance(value, tuple) else safe_items

    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, Mapping):
        return _safe_log_value(attributes, _seen=seen)
    return _redact(str(value))


def _safe_log_argument(value: object) -> object:
    """Prevent deferred logging interpolation from exposing nested values."""

    return _safe_log_value(value)


def _safe_traceback(record: logging.LogRecord) -> None:
    """Keep stack locations while removing all exception values and messages."""

    if record.exc_info is not None:
        exc_type, _exc_value, exc_tb = record.exc_info
        frames = "".join(traceback.format_tb(exc_tb)) if exc_tb is not None else ""
        type_name = getattr(exc_type, "__name__", "Exception")
        record.exc_text = f"{_redact(frames)}{type_name}: [REDACTED]"
        record.exc_info = None
    elif record.exc_text:
        record.exc_text = "[REDACTED EXCEPTION]"
    if record.stack_info:
        record.stack_info = _redact(record.stack_info)


class SensitiveDataFilter(logging.Filter):
    """Redact known credential patterns from log records before output."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _safe_log_value(record.msg)
        args: object = record.args
        if args:
            if isinstance(args, tuple):
                record.args = tuple(_safe_log_argument(arg) for arg in args)
            elif isinstance(args, Mapping):
                setattr(record, "args", _safe_log_value(args))
            elif isinstance(args, str):
                # LogRecord's annotation permits only tuple/mapping arguments,
                # but logging accepts arbitrary values at runtime.
                setattr(record, "args", _redact(args))
            elif isinstance(args, BaseException):
                setattr(record, "args", _safe_log_argument(args))
            else:
                setattr(record, "args", _safe_log_argument(args))

        for key, value in list(record.__dict__.items()):
            if key in _STANDARD_LOG_RECORD_ATTRIBUTES:
                continue
            record.__dict__[key] = (
                "[REDACTED]" if _is_sensitive_log_key(key) else _safe_log_value(value)
            )
        _safe_traceback(record)
        return True


def _parse_log_level(raw: str, default: int = logging.INFO) -> int:
    """Convert a level name string to a logging level integer.

    Falls back to *default* and writes a warning to stderr when *raw* is not
    a recognised level name.  stderr is used because the proxbox logger has not
    been constructed yet at the call site.
    """
    normalized = raw.strip().upper()
    if normalized not in _VALID_LEVELS:
        sys.stderr.write(f"proxbox: unknown PROXBOX_LOG_LEVEL={raw!r}, defaulting to INFO\n")
        return default
    return getattr(logging, normalized)


def _configure_third_party_levels(console_level: int) -> None:
    """Set the floor level for known noisy third-party loggers.

    When the operator requests DEBUG output, third-party loggers are left at
    DEBUG so full SDK tracing is visible.  Otherwise they are raised to WARNING
    so they do not flood INFO output.
    """
    floor = logging.DEBUG if console_level <= logging.DEBUG else logging.WARNING
    for name in _THIRD_PARTY_NOISY:
        logging.getLogger(name).setLevel(floor)


# Context variable for operation tracking
_operation_context: contextvars.ContextVar[dict[str, object]] = contextvars.ContextVar(
    "operation_context", default={}
)

P = ParamSpec("P")
R = TypeVar("R")


# ANSI escape sequences for colors
class AnsiColorCodes:
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    RESET = "\033[0m"
    DARK_GRAY = "\033[90m"


class ColorizedFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: AnsiColorCodes.BLUE,
        logging.INFO: AnsiColorCodes.GREEN,
        logging.WARNING: AnsiColorCodes.YELLOW,
        logging.ERROR: AnsiColorCodes.RED,
        logging.CRITICAL: AnsiColorCodes.MAGENTA,
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelno, AnsiColorCodes.WHITE)

        record.module = f"{AnsiColorCodes.DARK_GRAY}{record.module}{AnsiColorCodes.RESET}"

        record.levelname = f"{color}{record.levelname}{AnsiColorCodes.RESET}"
        return super().format(record)


def _create_file_handler(
    target_logger: logging.Logger,
    log_path: str,
    formatter: logging.Formatter,
) -> TimedRotatingFileHandler | None:
    """Build a rotating file handler for warning+ logs."""
    try:
        file_handler = TimedRotatingFileHandler(
            log_path, when="midnight", interval=1, backupCount=7
        )
    except OSError:
        target_logger.warning("Not able to create '%s' archive.", log_path)
        return None

    file_handler.setLevel(logging.WARNING)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(SensitiveDataFilter())
    return file_handler


def _remove_file_handlers(target_logger: logging.Logger) -> None:
    """Detach and close existing rotating file handlers."""
    for handler in list(target_logger.handlers):
        if isinstance(handler, TimedRotatingFileHandler):
            target_logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass


def configure_file_logging_path(
    log_path: str | None, target_logger: logging.Logger | None = None
) -> str | None:
    """Reconfigure proxbox file logging destination.

    Returns the path that was successfully applied, or ``None`` if no file handler
    could be installed.
    """
    logger_obj = target_logger or logger
    formatter: logging.Formatter | None = None
    for handler in logger_obj.handlers:
        if isinstance(handler, logging.StreamHandler):
            formatter = handler.formatter
            if formatter is not None:
                break
    if formatter is None:
        formatter = ColorizedFormatter(
            "%(name)s [%(asctime)s] [%(levelname)-8s] %(module)s: %(message)s"
        )

    desired_path = (log_path or "").strip()
    if not desired_path or not desired_path.startswith("/") or desired_path.endswith("/"):
        desired_path = DEFAULT_LOG_PATH

    _remove_file_handlers(logger_obj)
    file_handler = _create_file_handler(logger_obj, desired_path, formatter)
    if file_handler is not None:
        logger_obj.addHandler(file_handler)
        return desired_path

    if desired_path != DEFAULT_LOG_PATH:
        fallback_handler = _create_file_handler(logger_obj, DEFAULT_LOG_PATH, formatter)
        if fallback_handler is not None:
            logger_obj.addHandler(fallback_handler)
            return DEFAULT_LOG_PATH

    return None


def setup_logger() -> logging.Logger:
    app_logger = logging.getLogger("proxbox")

    # Logger itself accepts all levels; handlers decide what reaches the output.
    app_logger.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()

    # Console level is controlled by PROXBOX_LOG_LEVEL (default INFO).
    # The in-memory buffer and rotating file handler are unaffected: they
    # always receive DEBUG+ and WARNING+ respectively.
    console_level = _parse_log_level(os.environ.get("PROXBOX_LOG_LEVEL", "INFO"))
    console_handler.setLevel(console_level)
    console_handler.addFilter(SensitiveDataFilter())

    formatter = ColorizedFormatter(
        "%(name)s [%(asctime)s] [%(levelname)-8s] %(module)s: %(message)s"
    )
    console_handler.setFormatter(formatter)

    app_logger.addHandler(console_handler)
    configure_file_logging_path(DEFAULT_LOG_PATH, target_logger=app_logger)

    app_logger.propagate = False

    # Suppress known verbose third-party loggers unless the operator requested DEBUG.
    _configure_third_party_levels(console_level)

    return app_logger


logger = setup_logger()


class OperationLogger:
    """Context manager for operation-scoped logging with structured fields.

    Usage:
        async with OperationLogger("sync_vm", vm_id=123, cluster="prod"):
            # All logs within this context will include the operation context
            await sync_operation()
    """

    def __init__(self, operation: str, **context: object) -> None:
        """Initialize the operation logger.

        Args:
            operation: Name of the operation being performed
            **context: Additional context fields (vm_id, cluster, etc.)
        """
        self.operation = operation
        self.context = context
        self.start_time = 0.0
        self.previous_context: dict[str, object] = {}

    async def __aenter__(self) -> "OperationLogger":
        """Enter the context and set operation context."""
        self.previous_context = _operation_context.get()
        self.start_time = time.time()

        new_context = {"operation": self.operation, **self.context}
        _operation_context.set(new_context)

        logger.debug(f"Starting {self.operation}", extra=new_context)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Exit the context and log completion/failure."""
        elapsed = time.time() - self.start_time
        context = _operation_context.get()
        context["elapsed_seconds"] = round(elapsed, 3)

        if exc_type:
            logger.error(
                f"Failed {self.operation} after {elapsed:.3f}s",
                exc_info=True,
                extra=context,
            )
        else:
            logger.info(
                f"Completed {self.operation} in {elapsed:.3f}s",
                extra=context,
            )

        # Restore previous context
        _operation_context.set(self.previous_context)


def get_operation_context() -> dict[str, object]:
    """Get the current operation context.

    Returns:
        Dictionary with operation context fields
    """
    return _operation_context.get().copy()


def timed_operation(func: Callable[P, R]) -> Callable[P, R]:
    """Decorator to log operation timing for sync functions.

    Usage:
        @timed_operation
        def sync_devices():
            ...
    """

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        operation_name = func.__name__
        start_time = time.time()

        logger.debug(f"Starting {operation_name}")

        try:
            result = func(*args, **kwargs)
            elapsed = time.time() - start_time
            logger.debug(
                f"Completed {operation_name} in {elapsed:.3f}s",
                extra={"elapsed_seconds": round(elapsed, 3)},
            )
            return result
        except Exception as error:
            elapsed = time.time() - start_time
            logger.error(
                f"Failed {operation_name} after {elapsed:.3f}s: %s",
                error,
                exc_info=True,
                extra={"elapsed_seconds": round(elapsed, 3)},
            )
            raise

    return wrapper


def async_timed_operation(func: Callable[P, R]) -> Callable[P, R]:
    """Decorator to log operation timing for async functions.

    Usage:
        @async_timed_operation
        async def sync_vms():
            ...
    """

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        operation_name = func.__name__
        start_time = time.time()

        logger.debug(f"Starting {operation_name}")

        try:
            result = await func(*args, **kwargs)
            elapsed = time.time() - start_time
            logger.debug(
                f"Completed {operation_name} in {elapsed:.3f}s",
                extra={"elapsed_seconds": round(elapsed, 3)},
            )
            return result
        except Exception as error:
            elapsed = time.time() - start_time
            logger.error(
                f"Failed {operation_name} after {elapsed:.3f}s: %s",
                error,
                exc_info=True,
                extra={"elapsed_seconds": round(elapsed, 3)},
            )
            raise

    return wrapper  # type: ignore


async def log(websocket: WebSocket, msg: str, level: str | None = None) -> None:
    """Legacy log function for WebSocket + console logging.

    Args:
        websocket: WebSocket connection to send message to
        msg: Message to log
        level: Log level (debug, error, ERROR, or info)
    """
    if websocket:
        await websocket.send_text(msg)

    if level == "debug":
        logger.debug(msg)
    elif level in ("ERROR", "error"):
        logger.error(msg)
    else:
        logger.info(msg)

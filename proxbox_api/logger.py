"""Logging setup utilities for console and file outputs."""

import contextvars
import logging
import time
from collections.abc import Callable
from functools import wraps
from logging.handlers import TimedRotatingFileHandler
from typing import ParamSpec, TypeVar

from fastapi import WebSocket

from proxbox_api.constants import DEFAULT_LOG_PATH

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
    # Create a logger
    app_logger = logging.getLogger("proxbox")

    app_logger.setLevel(logging.DEBUG)

    # # Create a console handler
    console_handler = logging.StreamHandler()

    # Log all messages in the console
    console_handler.setLevel(logging.DEBUG)

    # Create a formatter with colors
    formatter = ColorizedFormatter(
        "%(name)s [%(asctime)s] [%(levelname)-8s] %(module)s: %(message)s"
    )
    # formatter = logging.Formatter('[%(asctime)s] [%(levelname)s] %(module)s: %(message)s')
    # Set the formatter for the console handler and file handler
    console_handler.setFormatter(formatter)

    # Add the handlers to the logger
    app_logger.addHandler(console_handler)
    configure_file_logging_path(DEFAULT_LOG_PATH, target_logger=app_logger)

    app_logger.propagate = False

    return app_logger


logger = setup_logger()


class OperationLogger:
    """Context manager for operation-scoped logging with structured fields.

    Usage:
        async with OperationLogger("sync_vm", vm_id=123, cluster="prod"):
            # All logs within this context will include the operation context
            await sync_operation()
    """

    def __init__(self, operation: str, **context: object):
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

        logger.info(f"Starting {self.operation}", extra=new_context)
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
                f"Failed {operation_name} after {elapsed:.3f}s: {error}",
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
                f"Failed {operation_name} after {elapsed:.3f}s: {error}",
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

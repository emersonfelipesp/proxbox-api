"""Logging setup utilities for console and file outputs."""

import contextvars
import logging
import time
from collections.abc import Callable
from functools import wraps
from logging.handlers import TimedRotatingFileHandler
from typing import Any, ParamSpec, TypeVar

from fastapi import WebSocket

# Context variable for operation tracking
_operation_context: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
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


def setup_logger() -> logging.Logger:
    # Path to log file
    log_path = "/var/log/proxbox.log"

    # Create a logger
    logger = logging.getLogger("proxbox")

    logger.setLevel(logging.DEBUG)

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

    file_handler: TimedRotatingFileHandler | None = None
    try:
        # Create a file handler
        file_handler = TimedRotatingFileHandler(
            log_path, when="midnight", interval=1, backupCount=7
        )

        # Log only WARNINGS and above in the file
        file_handler.setLevel(logging.WARNING)

        # Set the formatter for the file handler
        file_handler.setFormatter(formatter)
    except OSError:
        logger.warning("Not able to create '%s' archive.", log_path)

    # Add the handlers to the logger
    logger.addHandler(console_handler)
    if file_handler is not None:
        logger.addHandler(file_handler)

    logger.propagate = False

    return logger


logger = setup_logger()


class OperationLogger:
    """Context manager for operation-scoped logging with structured fields.

    Usage:
        async with OperationLogger("sync_vm", vm_id=123, cluster="prod"):
            # All logs within this context will include the operation context
            await sync_operation()
    """

    def __init__(self, operation: str, **context: Any):
        """Initialize the operation logger.

        Args:
            operation: Name of the operation being performed
            **context: Additional context fields (vm_id, cluster, etc.)
        """
        self.operation = operation
        self.context = context
        self.start_time = 0.0
        self.previous_context: dict[str, Any] = {}

    async def __aenter__(self) -> "OperationLogger":
        """Enter the context and set operation context."""
        self.previous_context = _operation_context.get()
        self.start_time = time.time()

        new_context = {"operation": self.operation, **self.context}
        _operation_context.set(new_context)

        logger.info(f"Starting {self.operation}", extra=new_context)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: Any
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


def get_operation_context() -> dict[str, Any]:
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

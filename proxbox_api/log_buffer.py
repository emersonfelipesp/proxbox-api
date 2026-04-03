"""In-memory log buffer for backend log retrieval via API."""

from __future__ import annotations

import logging
import threading
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


class LogLevel(str, Enum):
    """Log level enum matching Python logging levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @classmethod
    def from_python(cls, level: int) -> LogLevel:
        """Convert Python logging level int to LogLevel enum."""
        if level >= logging.CRITICAL:
            return cls.CRITICAL
        if level >= logging.ERROR:
            return cls.ERROR
        if level >= logging.WARNING:
            return cls.WARNING
        if level >= logging.INFO:
            return cls.INFO
        return cls.DEBUG

    @classmethod
    def from_string(cls, level: str) -> LogLevel:
        """Convert string to LogLevel enum (case-insensitive)."""
        return cls(level.upper())

    def to_python(self) -> int:
        """Convert LogLevel to Python logging level int."""
        return getattr(logging, self.name)


@dataclass
class BufferedLogEntry:
    """A single log entry stored in the buffer."""

    id: str
    timestamp: datetime
    level: LogLevel
    module: str
    message: str
    operation_id: str | None
    operation: str | None
    phase: str | None
    resource_type: str | None
    resource_id: str | int | None
    expandable: dict | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "level": self.level.value,
            "module": self.module,
            "message": self.message,
            "operation_id": self.operation_id,
            "operation": self.operation,
            "phase": self.phase,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "expandable": self.expandable,
        }


@dataclass
class LogBufferConfig:
    """Configuration for the log buffer."""

    capacity: int = 10000
    default_limit: int = 200
    max_limit: int = 5000


class LogBufferHandler(logging.Handler):
    """Custom logging handler that stores records in a thread-safe deque."""

    def __init__(self, capacity: int = 10000):
        """Initialize the handler with a circular buffer.

        Args:
            capacity: Maximum number of log entries to keep (default 10000)
        """
        super().__init__()
        self.buffer: deque[BufferedLogEntry] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._id_counter = 0

    def _next_id(self) -> str:
        """Generate a unique sequential ID for log entries."""
        self._id_counter += 1
        return str(self._id_counter)

    def emit(self, record: logging.LogRecord) -> None:
        """Process a log record and store it in the buffer.

        Args:
            record: Python logging LogRecord
        """
        try:
            extra = getattr(record, "extra", {}) or {}

            level = LogLevel.from_python(record.levelno)
            timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc)

            operation_id = extra.get("operation_id") or extra.get("uuid") or None
            operation = extra.get("operation") or None
            phase = extra.get("phase") or None
            resource_type = extra.get("resource_type") or None
            resource_id = extra.get("resource_id") or None

            expandable = None
            if record.exc_info:
                expandable = {
                    "traceback": "".join(traceback.format_exception(*record.exc_info)),
                }

            entry = BufferedLogEntry(
                id=self._next_id(),
                timestamp=timestamp,
                level=level,
                module=record.module,
                message=record.getMessage(),
                operation_id=operation_id,
                operation=operation,
                phase=phase,
                resource_type=resource_type,
                resource_id=resource_id,
                expandable=expandable,
            )

            with self._lock:
                self.buffer.append(entry)

        except Exception:
            self.handleError(record)

    def get_logs(
        self,
        level: str | None = None,
        limit: int = 200,
        offset: int = 0,
        since: datetime | None = None,
        operation_id: str | None = None,
    ) -> dict:
        """Retrieve logs from the buffer with optional filtering.

        Args:
            level: Minimum log level to include (as string: DEBUG, INFO, WARNING, ERROR, CRITICAL)
            limit: Maximum number of entries to return (default 200, max 5000)
            offset: Number of entries to skip (for pagination)
            since: Only return logs after this timestamp
            operation_id: Filter by specific operation ID

        Returns:
            Dictionary with logs, total count, has_more flag, and active_filters
        """
        config = LogBufferConfig()

        limit = min(limit, config.max_limit)
        if limit <= 0:
            limit = config.default_limit

        with self._lock:
            logs = list(self.buffer)

        filtered_logs: list[BufferedLogEntry] = []

        for entry in logs:
            if level:
                min_level = LogLevel.from_string(level)
                if entry.level.to_python() < min_level.to_python():
                    continue

            if since and entry.timestamp <= since:
                continue

            if operation_id and entry.operation_id != operation_id:
                continue

            filtered_logs.append(entry)

        total = len(filtered_logs)

        start_idx = offset
        end_idx = offset + limit
        paginated_logs = filtered_logs[start_idx:end_idx]

        has_more = end_idx < total

        active_filters: dict[str, str | None] = {
            "level": level,
            "operation_id": operation_id,
            "since": since.isoformat() if since else None,
        }

        return {
            "logs": [log.to_dict() for log in paginated_logs],
            "total": total,
            "has_more": has_more,
            "active_filters": active_filters,
        }

    def clear(self) -> None:
        """Clear all logs from the buffer."""
        with self._lock:
            self.buffer.clear()
            self._id_counter = 0

    @property
    def count(self) -> int:
        """Get current number of logs in buffer."""
        with self._lock:
            return len(self.buffer)


_global_log_buffer: LogBufferHandler | None = None
_global_log_buffer_lock = threading.Lock()


def get_log_buffer() -> LogBufferHandler:
    """Get the global log buffer instance (singleton).

    Returns:
        The global LogBufferHandler instance
    """
    global _global_log_buffer
    if _global_log_buffer is None:
        with _global_log_buffer_lock:
            if _global_log_buffer is None:
                _global_log_buffer = LogBufferHandler()
    return _global_log_buffer


def clear_log_buffer() -> None:
    """Clear the global log buffer."""
    get_log_buffer().clear()


def get_logs(
    level: str | None = None,
    limit: int = 200,
    offset: int = 0,
    since: datetime | None = None,
    operation_id: str | None = None,
) -> dict:
    """Convenience function to get logs from the global buffer.

    Args:
        level: Minimum log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        limit: Max entries to return
        offset: Pagination offset
        since: Only logs after this timestamp
        operation_id: Filter by operation ID

    Returns:
        Dictionary with logs, total, has_more, and active_filters
    """
    return get_log_buffer().get_logs(
        level=level,
        limit=limit,
        offset=offset,
        since=since,
        operation_id=operation_id,
    )


def configure_buffer_logger(logger_name: str = "proxbox", level: int = logging.DEBUG) -> None:
    """Configure the global log buffer handler on a logger.

    Args:
        logger_name: Name of the logger to attach the handler to
        level: Logging level to set on the logger
    """
    buffer = get_log_buffer()
    target_logger = logging.getLogger(logger_name)
    target_logger.addHandler(buffer)
    target_logger.setLevel(level)
    target_logger.propagate = False

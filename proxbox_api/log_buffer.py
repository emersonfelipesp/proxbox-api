"""In-memory log buffer for backend log retrieval via API."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


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


_ERROR_MESSAGE_RE = re.compile(
    r"\b(error|failed|failure|exception|traceback|fatal|critical)\b",
    re.IGNORECASE,
)

_PII_PATTERNS = [
    (
        re.compile(
            r'(?:token|password|secret|key|auth)[=:]\s*["\']?[a-zA-Z0-9_\-]{8,}["\']?',
            re.IGNORECASE,
        ),
        "[REDACTED]",
    ),
    (re.compile(r"Bearer\s+[a-zA-Z0-9_\-\.]+"), "Bearer [REDACTED]"),
    (re.compile(r"Basic\s+[a-zA-Z0-9+\/=]+"), "Basic [REDACTED]"),
    (
        re.compile(r"X-Proxbox-API-Key:\s*[a-zA-Z0-9]+", re.IGNORECASE),
        "X-Proxbox-API-Key: [REDACTED]",
    ),
]


def _redact_pii(text: str) -> str:
    """Redact personally identifiable information and secrets from text."""
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _format_percent_style_message(message: str, args: object) -> str | None:
    """Apply percent-style interpolation with tolerant args coercion."""
    if args in (None, (), [], {}):
        return message

    candidates: list[object] = []
    if isinstance(args, list):
        candidates.append(tuple(args))
    candidates.append(args)
    if not isinstance(args, (tuple, dict, list)):
        candidates.append((args,))

    for candidate in candidates:
        try:
            return message % candidate
        except Exception:  # noqa: BLE001
            continue
    return None


def _resolve_log_record_message(record: logging.LogRecord) -> str:
    """Resolve a record into display text without leaking format placeholders."""
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001
        raw_message = record.msg if isinstance(record.msg, str) else str(record.msg)
        rendered = _format_percent_style_message(raw_message, getattr(record, "args", None))
        message = rendered if rendered is not None else raw_message
    if not isinstance(message, str):
        return str(message)
    return message


def _entry_sort_key(entry: BufferedLogEntry) -> int:
    """Return a numeric sort key for a buffered log entry."""
    try:
        return int(entry.id)
    except (TypeError, ValueError):
        return -1


def _parse_cursor_id(value: int | str | None) -> int | None:
    """Parse a cursor ID from query input."""
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entry_is_error_related(entry: BufferedLogEntry) -> bool:
    """Return True when a log entry looks error-related."""
    if entry.level in {LogLevel.ERROR, LogLevel.CRITICAL}:
        return True

    if entry.expandable and entry.expandable.get("traceback"):
        return True

    return bool(_ERROR_MESSAGE_RE.search(entry.message))


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
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
        self._subscribers_lock = threading.Lock()

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
            level = LogLevel.from_python(record.levelno)
            timestamp = datetime.fromtimestamp(record.created, tz=timezone.utc)

            operation_id = getattr(record, "operation_id", None) or getattr(record, "uuid", None)
            operation = getattr(record, "operation", None)
            phase = getattr(record, "phase", None)
            resource_type = getattr(record, "resource_type", None)
            resource_id = getattr(record, "resource_id", None)

            expandable = None
            if record.exc_info:
                raw_traceback = "".join(traceback.format_exception(*record.exc_info))
                expandable = {
                    "traceback": _redact_pii(raw_traceback),
                }

            entry = BufferedLogEntry(
                id=self._next_id(),
                timestamp=timestamp,
                level=level,
                module=record.module,
                message=_redact_pii(_resolve_log_record_message(record)),
                operation_id=operation_id,
                operation=operation,
                phase=phase,
                resource_type=resource_type,
                resource_id=resource_id,
                expandable=expandable,
            )

            with self._lock:
                self.buffer.append(entry)

            self._notify_subscribers()

        except Exception:
            self.handleError(record)

    def _notify_subscribers(self) -> None:
        """Wake all SSE stream subscribers (called after each new entry)."""
        with self._subscribers_lock:
            subscribers = list(self._subscribers)
        for loop, event in subscribers:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass

    def subscribe(self, loop: asyncio.AbstractEventLoop, event: asyncio.Event) -> None:
        """Register an asyncio.Event to be set when new log entries arrive."""
        with self._subscribers_lock:
            self._subscribers.append((loop, event))

    def unsubscribe(self, event: asyncio.Event) -> None:
        """Remove a previously registered event."""
        with self._subscribers_lock:
            self._subscribers = [(lp, ev) for lp, ev in self._subscribers if ev is not event]

    @property
    def latest_id(self) -> int:
        """Return the current ID counter (highest assigned ID)."""
        with self._lock:
            return self._id_counter

    def get_logs(
        self,
        level: LogLevel | str | None = None,
        errors_only: bool = False,
        newer_than_id: int | str | None = None,
        older_than_id: int | str | None = None,
        limit: int = 200,
        offset: int = 0,
        since: datetime | None = None,
        operation_id: str | None = None,
    ) -> dict:
        """Retrieve logs from the buffer with optional filtering.

        Args:
            level: Exact log level to include (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            errors_only: Return only error-related logs, regardless of level
            newer_than_id: Return only logs with an ID greater than this entry ID
            older_than_id: Return only logs with an ID less than this entry ID
            limit: Maximum number of entries to return (default 200, max 5000)
            offset: Number of entries to skip (for pagination)
            since: Only return logs after this timestamp
            operation_id: Filter by specific operation ID

        Returns:
            Dictionary with logs, total count, has_more flag, and active_filters
        """
        config = LogBufferConfig()
        level_filter = None
        if level:
            level_filter = level if isinstance(level, LogLevel) else LogLevel.from_string(level)

        limit = min(limit, config.max_limit)
        if limit <= 0:
            limit = config.default_limit

        with self._lock:
            logs = list(self.buffer)

        logs.sort(key=_entry_sort_key, reverse=True)

        filtered_logs: list[BufferedLogEntry] = []
        newer_than_key = _parse_cursor_id(newer_than_id)
        older_than_key = _parse_cursor_id(older_than_id)

        for entry in logs:
            entry_key = _entry_sort_key(entry)

            if newer_than_key is not None and entry_key <= newer_than_key:
                continue

            if older_than_key is not None and entry_key >= older_than_key:
                continue

            if not errors_only and level_filter and entry.level != level_filter:
                continue

            if since and entry.timestamp <= since:
                continue

            if operation_id and entry.operation_id != operation_id:
                continue

            if errors_only and not _entry_is_error_related(entry):
                continue

            filtered_logs.append(entry)

        total = len(filtered_logs)

        start_idx = offset
        end_idx = offset + limit
        paginated_logs = filtered_logs[start_idx:end_idx]

        has_more = end_idx < total

        active_filters: dict[str, str | int | bool | None] = {
            "level": level_filter.value if level_filter else None,
            "errors_only": True if errors_only else None,
            "newer_than_id": newer_than_id,
            "older_than_id": older_than_id,
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
    level: LogLevel | str | None = None,
    errors_only: bool = False,
    newer_than_id: int | str | None = None,
    older_than_id: int | str | None = None,
    limit: int = 200,
    offset: int = 0,
    since: datetime | None = None,
    operation_id: str | None = None,
) -> dict:
    """Convenience function to get logs from the global buffer.

    Args:
        level: Exact log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        errors_only: Return only error-related logs, regardless of level
        newer_than_id: Return only logs newer than the given entry ID
        older_than_id: Return only logs older than the given entry ID
        limit: Max entries to return
        offset: Pagination offset
        since: Only logs after this timestamp
        operation_id: Filter by operation ID

    Returns:
        Dictionary with logs, total, has_more, and active_filters
    """
    return get_log_buffer().get_logs(
        level=level,
        errors_only=errors_only,
        newer_than_id=newer_than_id,
        older_than_id=older_than_id,
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
    if buffer not in target_logger.handlers:
        target_logger.addHandler(buffer)
    target_logger.setLevel(level)
    target_logger.propagate = False

"""Tests for in-memory log buffering and filtering utilities."""

from __future__ import annotations

import logging
import sys

from proxbox_api.log_buffer import (
    LogBufferHandler,
    LogLevel,
    configure_buffer_logger,
)


def _make_record(
    level: int,
    message: str,
    *,
    args: object = (),
    operation_id: str | None = None,
    operation: str | None = None,
    phase: str | None = None,
    resource_type: str | None = None,
    resource_id: str | int | None = None,
    exc_info=None,
) -> logging.LogRecord:
    record = logging.LogRecord(
        name="proxbox.sync",
        level=level,
        pathname="/tmp/sync.py",
        lineno=12,
        msg=message,
        args=args,
        exc_info=exc_info,
    )
    if operation_id is not None:
        record.operation_id = operation_id
    if operation is not None:
        record.operation = operation
    if phase is not None:
        record.phase = phase
    if resource_type is not None:
        record.resource_type = resource_type
    if resource_id is not None:
        record.resource_id = resource_id
    return record


def test_emit_captures_structured_fields_from_log_record():
    handler = LogBufferHandler()

    try:
        raise ValueError("boom")
    except ValueError:
        exc_info = sys.exc_info()

    handler.emit(
        _make_record(
            logging.INFO,
            "sync started",
            operation_id="op-123",
            operation="device_sync",
            phase="processing",
            resource_type="device",
            resource_id=42,
            exc_info=exc_info,
        )
    )

    assert handler.count == 1
    entry = handler.buffer[-1]
    assert entry.level == LogLevel.INFO
    assert entry.module == "sync"
    assert entry.message == "sync started"
    assert entry.operation_id == "op-123"
    assert entry.operation == "device_sync"
    assert entry.phase == "processing"
    assert entry.resource_type == "device"
    assert entry.resource_id == 42
    assert entry.expandable and "ValueError: boom" in entry.expandable["traceback"]


def test_emit_renders_percent_templates_with_tuple_args():
    handler = LogBufferHandler()

    handler.emit(
        _make_record(
            logging.INFO,
            "Stage completed: %s (HTTP %s)",
            args=("devices", 200),
        )
    )

    assert handler.count == 1
    entry = handler.buffer[-1]
    assert entry.message == "Stage completed: devices (HTTP 200)"
    assert "%s" not in entry.message


def test_emit_renders_percent_templates_with_list_args():
    handler = LogBufferHandler()

    handler.emit(
        _make_record(
            logging.INFO,
            "Starting stage: %s (%s)",
            args=["devices", "dcim/devices/create/stream"],
        )
    )

    assert handler.count == 1
    entry = handler.buffer[-1]
    assert entry.message == "Starting stage: devices (dcim/devices/create/stream)"
    assert "%s" not in entry.message


def test_get_logs_filters_by_exact_level_and_operation_id():
    handler = LogBufferHandler()

    handler.emit(_make_record(logging.DEBUG, "debug", operation_id="op-1"))
    handler.emit(_make_record(logging.INFO, "info", operation_id="op-1"))
    handler.emit(_make_record(logging.WARNING, "warning", operation_id="op-2"))

    info_only = handler.get_logs(level=LogLevel.INFO)
    assert [log["level"] for log in info_only["logs"]] == ["INFO"]
    assert info_only["total"] == 1

    op_only = handler.get_logs(operation_id="op-1")
    assert [log["message"] for log in op_only["logs"]] == ["info", "debug"]
    assert op_only["total"] == 2

    combined = handler.get_logs(level="WARNING", operation_id="op-2")
    assert [log["level"] for log in combined["logs"]] == ["WARNING"]
    assert combined["active_filters"]["level"] == "WARNING"
    assert combined["active_filters"]["operation_id"] == "op-2"


def test_get_logs_returns_newest_entries_first_and_supports_id_cursors():
    handler = LogBufferHandler()

    handler.emit(_make_record(logging.DEBUG, "debug"))
    handler.emit(_make_record(logging.INFO, "info"))
    handler.emit(_make_record(logging.WARNING, "warning"))

    newest_first = handler.get_logs(limit=2)
    assert [log["message"] for log in newest_first["logs"]] == ["warning", "info"]

    older_than_warning = handler.get_logs(older_than_id="3", limit=2)
    assert [log["message"] for log in older_than_warning["logs"]] == ["info", "debug"]

    newer_than_debug = handler.get_logs(newer_than_id="1", limit=2)
    assert [log["message"] for log in newer_than_debug["logs"]] == ["warning", "info"]


def test_get_logs_errors_only_includes_error_related_logs_across_levels():
    handler = LogBufferHandler()

    handler.emit(_make_record(logging.INFO, "sync started"))
    handler.emit(_make_record(logging.INFO, "Failed to load NetBox endpoint"))
    handler.emit(_make_record(logging.WARNING, "retrying after failure"))
    handler.emit(_make_record(logging.ERROR, "unexpected error"))

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        handler.emit(_make_record(logging.INFO, "traceback entry", exc_info=sys.exc_info()))

    errors_only = handler.get_logs(errors_only=True)

    assert [log["message"] for log in errors_only["logs"]] == [
        "traceback entry",
        "unexpected error",
        "retrying after failure",
        "Failed to load NetBox endpoint",
    ]
    assert [log["level"] for log in errors_only["logs"]] == [
        "INFO",
        "ERROR",
        "WARNING",
        "INFO",
    ]
    assert errors_only["active_filters"]["errors_only"] is True


def test_configure_buffer_logger_is_idempotent():
    logger = logging.getLogger("proxbox.test.log-buffer")
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate

    try:
        logger.handlers.clear()

        configure_buffer_logger(logger.name)
        configure_buffer_logger(logger.name)

        handlers = [handler for handler in logger.handlers if isinstance(handler, LogBufferHandler)]
        assert len(handlers) == 1
    finally:
        logger.handlers[:] = original_handlers
        logger.setLevel(original_level)
        logger.propagate = original_propagate

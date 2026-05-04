"""Tests for backend file log destination configuration."""

from __future__ import annotations

import logging

import proxbox_api.logger as logger_module
from proxbox_api.constants import DEFAULT_LOG_PATH
from proxbox_api.logger import configure_file_logging_path, logger


class _FakeTimedRotatingFileHandler(logging.Handler):
    created_paths: list[str] = []
    fail_paths: set[str] = set()

    def __init__(self, path: str, when="midnight", interval: int = 1, backupCount: int = 7):
        if path in type(self).fail_paths:
            raise OSError("simulated create failure")
        super().__init__()
        self.path = path
        type(self).created_paths.append(path)

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - noop
        return None


def test_configure_file_logging_path_uses_provided_absolute_path(monkeypatch):
    monkeypatch.setattr(logger_module, "TimedRotatingFileHandler", _FakeTimedRotatingFileHandler)

    original_handlers = list(logger.handlers)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [stream_handler]
    _FakeTimedRotatingFileHandler.created_paths = []
    _FakeTimedRotatingFileHandler.fail_paths = set()

    try:
        applied = configure_file_logging_path("/tmp/proxbox-custom.log")

        assert applied == "/tmp/proxbox-custom.log"
        assert _FakeTimedRotatingFileHandler.created_paths == ["/tmp/proxbox-custom.log"]
        file_handlers = [
            handler
            for handler in logger.handlers
            if isinstance(handler, _FakeTimedRotatingFileHandler)
        ]
        assert len(file_handlers) == 1
        assert file_handlers[0].path == "/tmp/proxbox-custom.log"
    finally:
        logger.handlers = original_handlers


def test_configure_file_logging_path_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(logger_module, "TimedRotatingFileHandler", _FakeTimedRotatingFileHandler)

    original_handlers = list(logger.handlers)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers = [stream_handler]
    _FakeTimedRotatingFileHandler.created_paths = []
    _FakeTimedRotatingFileHandler.fail_paths = {"/tmp/deny.log"}

    try:
        applied = configure_file_logging_path("/tmp/deny.log")

        assert applied == DEFAULT_LOG_PATH
        assert _FakeTimedRotatingFileHandler.created_paths == [DEFAULT_LOG_PATH]

        applied = configure_file_logging_path("relative/path.log")
        assert applied == DEFAULT_LOG_PATH
        assert _FakeTimedRotatingFileHandler.created_paths[-1] == DEFAULT_LOG_PATH
    finally:
        logger.handlers = original_handlers


def test_bootstrap_applies_backend_log_file_path_from_settings(monkeypatch):
    from proxbox_api.app import bootstrap

    captured: dict[str, str] = {}
    monkeypatch.setattr(
        bootstrap,
        "get_settings",
        lambda netbox_session=None, use_cache=False: {
            "backend_log_file_path": "/tmp/from-settings.log"
        },
    )
    monkeypatch.setattr(
        bootstrap,
        "configure_file_logging_path",
        lambda path: captured.setdefault("path", path),
    )
    bootstrap.netbox_session = object()

    bootstrap._configure_backend_file_logging()

    assert captured["path"] == "/tmp/from-settings.log"

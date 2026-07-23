"""Tests for backend file log destination and console level configuration."""

from __future__ import annotations

import io
import logging

import proxbox_api.logger as logger_module
from proxbox_api.constants import DEFAULT_LOG_PATH
from proxbox_api.logger import (
    SensitiveDataFilter,
    _configure_third_party_levels,
    _parse_log_level,
    configure_file_logging_path,
    logger,
)


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


# ---------------------------------------------------------------------------
# PROXBOX_LOG_LEVEL env var and console handler level
# ---------------------------------------------------------------------------


def test_parse_log_level_defaults_to_info_when_env_not_set():
    assert _parse_log_level("INFO") == logging.INFO


def test_parse_log_level_accepts_debug():
    assert _parse_log_level("DEBUG") == logging.DEBUG


def test_parse_log_level_is_case_insensitive():
    assert _parse_log_level("debug") == logging.DEBUG
    assert _parse_log_level("Warning") == logging.WARNING


def test_parse_log_level_falls_back_to_info_for_unknown_value(capsys):
    result = _parse_log_level("NONSENSE")
    assert result == logging.INFO
    captured = capsys.readouterr()
    assert "PROXBOX_LOG_LEVEL" in captured.err
    assert "NONSENSE" in captured.err


def test_console_handler_level_defaults_to_info_without_env_var(monkeypatch):
    """setup_logger() must set the console handler to INFO when PROXBOX_LOG_LEVEL is absent."""
    monkeypatch.delenv("PROXBOX_LOG_LEVEL", raising=False)
    monkeypatch.setattr(logger_module, "TimedRotatingFileHandler", _FakeTimedRotatingFileHandler)

    original_handlers = list(logger.handlers)
    logger.handlers = []
    _FakeTimedRotatingFileHandler.created_paths = []
    _FakeTimedRotatingFileHandler.fail_paths = set()
    try:
        result_logger = logger_module.setup_logger()
        console_handlers = [
            h
            for h in result_logger.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        assert console_handlers, "expected at least one StreamHandler"
        assert console_handlers[0].level == logging.INFO
    finally:
        logger.handlers = original_handlers


def test_console_handler_level_follows_proxbox_log_level_env_var(monkeypatch):
    """PROXBOX_LOG_LEVEL=DEBUG must lower the console handler to DEBUG."""
    monkeypatch.setenv("PROXBOX_LOG_LEVEL", "DEBUG")
    monkeypatch.setattr(logger_module, "TimedRotatingFileHandler", _FakeTimedRotatingFileHandler)

    original_handlers = list(logger.handlers)
    logger.handlers = []
    _FakeTimedRotatingFileHandler.created_paths = []
    _FakeTimedRotatingFileHandler.fail_paths = set()
    try:
        result_logger = logger_module.setup_logger()
        console_handlers = [
            h
            for h in result_logger.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        assert console_handlers, "expected at least one StreamHandler"
        assert console_handlers[0].level == logging.DEBUG
    finally:
        logger.handlers = original_handlers


def test_invalid_proxbox_log_level_falls_back_to_info(monkeypatch, capsys):
    """An unrecognised PROXBOX_LOG_LEVEL must fall back to INFO and emit a stderr warning."""
    monkeypatch.setenv("PROXBOX_LOG_LEVEL", "BANANAS")
    monkeypatch.setattr(logger_module, "TimedRotatingFileHandler", _FakeTimedRotatingFileHandler)

    original_handlers = list(logger.handlers)
    logger.handlers = []
    _FakeTimedRotatingFileHandler.created_paths = []
    _FakeTimedRotatingFileHandler.fail_paths = set()
    try:
        result_logger = logger_module.setup_logger()
        console_handlers = [
            h
            for h in result_logger.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        assert console_handlers[0].level == logging.INFO
        err = capsys.readouterr().err
        assert "PROXBOX_LOG_LEVEL" in err
    finally:
        logger.handlers = original_handlers


# ---------------------------------------------------------------------------
# Third-party logger suppression
# ---------------------------------------------------------------------------


def test_third_party_loggers_suppressed_to_warning_at_info_level():
    """`_configure_third_party_levels(INFO)` must raise netbox_sdk.client to WARNING."""
    nb_client_logger = logging.getLogger("netbox_sdk.client")
    original_level = nb_client_logger.level
    try:
        _configure_third_party_levels(logging.INFO)
        assert nb_client_logger.level == logging.WARNING
    finally:
        nb_client_logger.setLevel(original_level)


def test_third_party_loggers_not_suppressed_at_debug_level():
    """`_configure_third_party_levels(DEBUG)` must leave netbox_sdk.client at DEBUG."""
    nb_client_logger = logging.getLogger("netbox_sdk.client")
    original_level = nb_client_logger.level
    try:
        _configure_third_party_levels(logging.DEBUG)
        assert nb_client_logger.level == logging.DEBUG
    finally:
        nb_client_logger.setLevel(original_level)


# ---------------------------------------------------------------------------
# Sensitive data redaction
# ---------------------------------------------------------------------------


def test_sensitive_data_filter_redacts_headers_and_secret_pairs():
    record = logging.LogRecord(
        name="proxbox",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=(
            "Authorization: Bearer auth-token X-Proxbox-API-Key: proxbox-key "
            'password="db-password" token_value=token-secret '
            '{"client_secret": "json-secret", "api_key": "json-key"} '
            "Bearer loose-token"
        ),
        args=(),
        exc_info=None,
    )

    assert SensitiveDataFilter().filter(record) is True
    message = record.getMessage()

    for leaked in (
        "auth-token",
        "proxbox-key",
        "db-password",
        "token-secret",
        "json-secret",
        "json-key",
        "loose-token",
    ):
        assert leaked not in message
    assert "[REDACTED]" in message


def test_sensitive_data_filter_redacts_string_args():
    record = logging.LogRecord(
        name="proxbox",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="headers=%s payload=%s",
        args=("Bearer arg-token", '{"password": "arg-password"}'),
        exc_info=None,
    )

    SensitiveDataFilter().filter(record)
    message = record.getMessage()

    assert "arg-token" not in message
    assert "arg-password" not in message
    assert "Bearer [REDACTED]" in message

    mapping_record = logging.LogRecord(
        name="proxbox",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="password=%(password)s",
        args=({"password": "mapping-arg-canary"},),
        exc_info=None,
    )
    SensitiveDataFilter().filter(mapping_record)
    assert "mapping-arg-canary" not in mapping_record.getMessage()


def test_sensitive_data_filter_scrubs_nested_extras_urls_and_tracebacks_through_handler():
    """A real handler/formatter must never render nested or exception canaries."""

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SensitiveDataFilter())
    handler.setFormatter(logging.Formatter("%(message)s details=%(details)s"))
    test_logger = logging.getLogger("proxbox.test.recursive-redaction")
    original_handlers = list(test_logger.handlers)
    original_level = test_logger.level
    original_propagate = test_logger.propagate
    test_logger.handlers = [handler]
    test_logger.setLevel(logging.ERROR)
    test_logger.propagate = False

    direct_url = (
        "https://operator:url-canary@pve.invalid?api_token=query-canary"
        "&accessToken=query-access-token-canary&client-secret=query-client-secret-canary"
    )
    try:
        try:
            raise RuntimeError(
                "https://trace-user:trace-canary@pve.invalid?password=trace-query-canary"
            )
        except RuntimeError:
            test_logger.error(
                "payload=%s url=%s assignments=%s",
                {
                    "outer": {
                        "clientSecret": "nested-canary",
                        "failure": RuntimeError("exception-canary"),
                    }
                },
                direct_url,
                (
                    "apiToken: assignment-api-token-canary "
                    "access_token=assignment-access-token-canary "
                    "clientSecret: assignment-client-secret-canary"
                ),
                exc_info=True,
                extra={
                    "details": {
                        "access-key": "extra-canary",
                        "safe": direct_url,
                    }
                },
            )
    finally:
        test_logger.handlers = original_handlers
        test_logger.setLevel(original_level)
        test_logger.propagate = original_propagate

    rendered = stream.getvalue()
    for canary in (
        "url-canary",
        "query-canary",
        "query-access-token-canary",
        "query-client-secret-canary",
        "assignment-api-token-canary",
        "assignment-access-token-canary",
        "assignment-client-secret-canary",
        "nested-canary",
        "exception-canary",
        "trace-canary",
        "trace-query-canary",
        "extra-canary",
    ):
        assert canary not in rendered
    assert rendered.count("[REDACTED]") >= 5


def test_setup_logger_attaches_sensitive_data_filter_to_console_and_file(monkeypatch):
    monkeypatch.setattr(logger_module, "TimedRotatingFileHandler", _FakeTimedRotatingFileHandler)

    original_handlers = list(logger.handlers)
    logger.handlers = []
    _FakeTimedRotatingFileHandler.created_paths = []
    _FakeTimedRotatingFileHandler.fail_paths = set()
    try:
        result_logger = logger_module.setup_logger()
        console_handlers = [
            h
            for h in result_logger.handlers
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        ]
        file_handlers = [
            h for h in result_logger.handlers if isinstance(h, _FakeTimedRotatingFileHandler)
        ]

        assert console_handlers
        assert file_handlers
        assert any(isinstance(f, SensitiveDataFilter) for f in console_handlers[0].filters)
        assert any(isinstance(f, SensitiveDataFilter) for f in file_handlers[0].filters)
    finally:
        logger.handlers = original_handlers


def test_admin_log_buffer_keeps_redacted_tracebacks_behind_the_sensitive_filter():
    """The buffer handler must still capture tracebacks after the filter runs.

    Production registration order is console/file handlers (each carrying
    ``SensitiveDataFilter``) first, then ``LogBufferHandler`` appended by
    ``configure_buffer_logger()``. A single ``LogRecord`` is shared across all
    of them, so the filter must not destroy ``exc_info``/``exc_text`` state the
    buffer needs — a regression here silently deletes every traceback from
    ``/admin/logs`` app-wide.
    """

    from proxbox_api.log_buffer import LogBufferHandler

    stream = io.StringIO()
    console = logging.StreamHandler(stream)
    console.addFilter(SensitiveDataFilter())
    console.setFormatter(logging.Formatter("%(message)s"))

    buffer_handler = LogBufferHandler()

    test_logger = logging.getLogger("proxbox.test.buffer-traceback-order")
    original_handlers = list(test_logger.handlers)
    original_level = test_logger.level
    original_propagate = test_logger.propagate
    # Same order as production: filter-carrying handler first, buffer second.
    test_logger.handlers = [console, buffer_handler]
    test_logger.setLevel(logging.ERROR)
    test_logger.propagate = False

    try:
        try:
            raise RuntimeError("secret-password=hunter2-canary")
        except RuntimeError:
            test_logger.error("operation failed: %s", "boom", exc_info=True)
    finally:
        test_logger.handlers = original_handlers
        test_logger.setLevel(original_level)
        test_logger.propagate = original_propagate

    assert buffer_handler.count == 1
    entry = buffer_handler.buffer[-1]
    assert entry.expandable is not None, (
        "the buffer lost the traceback: the sensitive filter must not null "
        "the shared record's exception state"
    )
    trace = entry.expandable["traceback"]
    assert "test_logger_settings" in trace or "RuntimeError" in trace
    assert "hunter2-canary" not in trace
    assert "hunter2-canary" not in stream.getvalue()

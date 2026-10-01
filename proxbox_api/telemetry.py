"""Protect native telemetry payloads before environment exporters are attached."""

import os
import threading
from weakref import WeakSet

from fastapi.telemetry import TelemetryConfig
from opentelemetry import _logs, trace
from opentelemetry._logs._internal import ProxyLoggerProvider
from opentelemetry.context import Context
from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor, ReadWriteLogRecord
from opentelemetry.sdk.trace import Span, SpanProcessor, TracerProvider

_log_providers: WeakSet[LoggerProvider] = WeakSet()
_trace_providers: WeakSet[TracerProvider] = WeakSet()
_lock = threading.RLock()


class ExceptionPrivacyProcessor(LogRecordProcessor):
    """Keep error classification without exception messages or stack traces."""

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        if log_record.instrumentation_scope and log_record.instrumentation_scope.name == "fastapi":
            original = log_record.log_record.attributes
            if original is not None:
                attributes = dict(original)
                attributes.pop("exception.message", None)
                attributes.pop("exception.stacktrace", None)
                log_record.log_record.attributes = attributes

    def shutdown(self) -> None:
        """No resources are owned by this processor."""

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        return True


class RequestPrivacyProcessor(SpanProcessor):
    """Redact concrete paths and query values before exporters observe spans."""

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:  # noqa: ARG002
        scope = span.instrumentation_scope
        if not scope or scope.name != "fastapi":
            return
        for key in ("url.path", "url.query"):
            if key in (span.attributes or {}):
                span.set_attribute(key, "REDACTED")

    def shutdown(self) -> None:
        """No resources are owned by this processor."""

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        return True


def _environment_export(config: TelemetryConfig, signal: str) -> bool:
    return (
        config.get("auto_configure", True)
        and (os.getenv(f"OTEL_{signal}_EXPORTER") or "otlp").strip().lower() != "none"
        and bool(
            os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
            or os.getenv(f"OTEL_EXPORTER_OTLP_{signal}_ENDPOINT")
        )
    )


def _configure_logs(config: TelemetryConfig) -> None:
    logger = config.get("logger_provider") or _logs.get_logger_provider()
    if isinstance(logger, ProxyLoggerProvider) and _environment_export(config, "LOGS"):
        logger = LoggerProvider()
        _logs.set_logger_provider(logger)
        logger = _logs.get_logger_provider()
    if isinstance(logger, LoggerProvider) and logger not in _log_providers:
        logger.add_log_record_processor(ExceptionPrivacyProcessor())
        _log_providers.add(logger)


def _configure_traces(config: TelemetryConfig) -> None:
    tracer = config.get("tracer_provider") or trace.get_tracer_provider()
    if isinstance(tracer, trace.ProxyTracerProvider) and _environment_export(config, "TRACES"):
        tracer = TracerProvider()
        trace.set_tracer_provider(tracer)
        tracer = trace.get_tracer_provider()
    if isinstance(tracer, TracerProvider) and tracer not in _trace_providers:
        tracer.add_span_processor(RequestPrivacyProcessor())
        _trace_providers.add(tracer)


def configure_telemetry_privacy(config: TelemetryConfig | None = None) -> None:
    """Register before native lifespan setup without replacing caller providers."""
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    settings: TelemetryConfig = config if config is not None else {}
    with _lock:
        if settings.get("logs", True):
            _configure_logs(settings)
        if settings.get("tracing", True):
            _configure_traces(settings)

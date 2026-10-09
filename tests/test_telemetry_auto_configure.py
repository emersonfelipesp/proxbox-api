"""Native opt-in precedence through the three shipping application factories."""

from collections.abc import Callable
from contextlib import ExitStack

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import _logs, metrics, trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from proxmox_mock.app import create_mock_app

from proxbox_api.app.factory import create_app
from proxbox_api.firecracker_agent.app import create_firecracker_agent_app

FACTORIES = (create_app, create_firecracker_agent_app, create_mock_app)
CASES = (
    pytest.param(None, None, False, False, id="public-default-off"),
    pytest.param("true", None, False, True, id="environment-opt-in"),
    pytest.param("TRUE", None, False, True, id="native-case-insensitive-opt-in"),
    pytest.param("1", None, False, False, id="native-literal-true-only"),
    pytest.param("true", False, False, False, id="explicit-false-wins"),
    pytest.param(None, True, False, True, id="explicit-opt-in"),
    pytest.param("true", None, True, False, id="sdk-disabled-environment"),
    pytest.param(None, True, True, False, id="sdk-disabled-explicit"),
)


def caller_providers(stack: ExitStack) -> tuple[dict, InMemorySpanExporter]:
    """Construct enabled caller providers before the SDK-disabled environment."""
    spans = InMemorySpanExporter()
    tracer = TracerProvider()
    stack.callback(tracer.shutdown)
    tracer.add_span_processor(SimpleSpanProcessor(spans))
    meter = MeterProvider()
    stack.callback(meter.shutdown)
    logger = LoggerProvider()
    stack.callback(logger.shutdown)
    return {
        "tracer_provider": tracer,
        "meter_provider": meter,
        "logger_provider": logger,
    }, spans


def select_environment(monkeypatch, environment: str | None, disabled: bool) -> None:
    """Use only synthetic operator inputs and exact native flag semantics."""
    monkeypatch.delenv("FASTAPI_OTEL_AUTO_CONFIGURE", raising=False)
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    if environment is not None:
        monkeypatch.setenv("FASTAPI_OTEL_AUTO_CONFIGURE", environment)
    if disabled:
        monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    # The endpoint alone must not enable native automatic exporter configuration.
    # No lifespan or exporter startup runs in this factory/request control.
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:9")


def exercise_request(app: FastAPI, stack: ExitStack) -> None:
    """Drive actual native middleware without the unrelated service lifespan."""

    @app.get("/_auto-configure/{number}")
    async def number(number: int) -> dict[str, int]:
        return {"number": number}

    client = TestClient(app)
    stack.callback(client.close)
    response = client.get("/_auto-configure/7?token=private-opt-in-canary")
    assert response.status_code == 200
    assert response.json() == {"number": 7}


def assert_selected_controls(app: FastAPI, providers: dict, expected: bool) -> None:
    """Check native effective controls and exact selected provider identities."""
    assert app._telemetry["auto_configure"] is expected
    assert all(app._telemetry[key] is provider for key, provider in providers.items())


def assert_native_request_privacy(spans: InMemorySpanExporter, disabled: bool) -> None:
    """Check only actual native server spans and SDK-disabled request suppression."""
    finished = [span for span in spans.get_finished_spans() if span.kind is SpanKind.SERVER]
    assert len(finished) == (0 if disabled else 1)
    assert "private-opt-in-canary" not in repr([dict(span.attributes) for span in finished])


@pytest.mark.parametrize("factory", FACTORIES)
@pytest.mark.parametrize("environment, explicit, disabled, expected", CASES)
def test_shipping_factory_native_opt_in_preserves_callers(
    factory: Callable[..., FastAPI],
    environment: str | None,
    explicit: bool | None,
    disabled: bool,
    expected: bool,
    monkeypatch,
) -> None:
    """Assert real native config, requests, privacy and caller lifecycle ownership."""
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.setattr("proxbox_api.app.factory.AUTH_EXEMPT_PATHS", {"/_auto-configure/7"})
    global_before = (
        trace.get_tracer_provider(),
        metrics.get_meter_provider(),
        _logs.get_logger_provider(),
    )
    with ExitStack() as stack:
        providers, spans = caller_providers(stack)
        select_environment(monkeypatch, environment, disabled)
        caller = dict(providers)
        if explicit is not None:
            caller["auto_configure"] = explicit
        original = dict(caller)
        app = factory(telemetry=caller)
        assert caller == original
        assert_selected_controls(app, providers, expected)
        assert global_before == (
            trace.get_tracer_provider(),
            metrics.get_meter_provider(),
            _logs.get_logger_provider(),
        )
        exercise_request(app, stack)
        assert_native_request_privacy(spans, disabled)
        caller_tracer = providers["tracer_provider"].get_tracer("caller-ownership")
        with caller_tracer.start_as_current_span("alive") as span:
            assert span.is_recording()

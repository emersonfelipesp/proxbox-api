"""Native telemetry contracts for all repository application factories."""

from collections.abc import Callable

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from proxmox_mock.app import create_mock_app

from proxbox_api.app.factory import create_app
from proxbox_api.firecracker_agent.app import create_firecracker_agent_app

FACTORIES = (create_app, create_firecracker_agent_app, create_mock_app)


@pytest.mark.parametrize("factory", FACTORIES)
def test_native_signals_preserve_explicit_provider_ownership(
    factory: Callable[..., FastAPI], monkeypatch
) -> None:
    """Exercise native HTTP, validation logs, metrics and WebSocket traces."""
    monkeypatch.setattr(
        "proxbox_api.app.factory.AUTH_EXEMPT_PATHS",
        {"/_telemetry-test/7", "/_telemetry-test/8", "/_telemetry-test/invalid"},
    )
    spans = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(spans))
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader])
    records = InMemoryLogRecordExporter()
    logs = LoggerProvider()
    logs.add_log_record_processor(SimpleLogRecordProcessor(records))
    app = factory(
        telemetry={
            "auto_configure": False,
            "tracer_provider": tracer,
            "meter_provider": meter,
            "logger_provider": logs,
        }
    )

    @app.get("/_telemetry-test/{number}")
    async def read_number(number: int) -> dict[str, int]:
        return {"number": number}

    @app.websocket("/_telemetry-test/ws")
    async def socket(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("ready")
        await websocket.close()

    # Avoid startup of real providers: the full production lifespan is verified
    # separately by the factory/auth/interactive/console regression suites.
    client = TestClient(app)
    try:
        assert client.get("/_telemetry-test/7?token=query-secret").json() == {"number": 7}
        assert client.get("/_telemetry-test/invalid").status_code == 422
        with client.websocket_connect("/_telemetry-test/ws") as connection:
            assert connection.receive_text() == "ready"
        finished = spans.get_finished_spans()
        assert sum(span.name == "GET /_telemetry-test/{number}" for span in finished) == 2
        assert sum(span.name == "WS /_telemetry-test/ws" for span in finished) == 1
        assert "query-secret" not in repr([dict(span.attributes) for span in finished])
        assert any(span.attributes.get("url.query") == "REDACTED" for span in finished)
        assert any("dependencies" in span.name for span in finished)
        assert records.get_finished_logs()
        data = reader.get_metrics_data()
        names = {
            metric.name
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        assert "http.server.request.duration" in names
        # Calling the app repeatedly never shuts down caller-owned providers.
        assert client.get("/_telemetry-test/8").status_code == 200
        assert len(spans.get_finished_spans()) > len(finished)
    finally:
        client.close()
        tracer.shutdown()
        meter.shutdown()
        logs.shutdown()


@pytest.mark.parametrize("module", ("proxbox_api.firecracker_agent.app", "proxmox_mock.app"))
@pytest.mark.parametrize("provider_mode", ("global", "explicit"))
def test_native_environment_export_is_opt_in(module: str, provider_mode: str, tmp_path) -> None:
    """Prove real OTLP protobuf export and no endpoint export in fresh processes."""
    import os
    import subprocess
    import sys

    probe = tmp_path / "probe.py"
    probe.write_text(
        """import importlib
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from fastapi import Body, WebSocket
from fastapi.testclient import TestClient
from opentelemetry import _logs, metrics, trace
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest

payloads = {}
class Collector(BaseHTTPRequestHandler):
    def do_POST(self):
        payloads.setdefault(self.path, []).append(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(200)
        self.end_headers()
    def log_message(self, *args):
        pass
server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
if sys.argv[2] == "enabled":
    os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"http://127.0.0.1:{server.server_port}"
    os.environ["FASTAPI_OTEL_AUTO_CONFIGURE"] = "true"
os.environ["OTEL_SERVICE_NAME"] = "telemetry-test"
module = importlib.import_module(sys.argv[1])
factory = getattr(module, "create_mock_app", None) or module.create_firecracker_agent_app
before = (trace.get_tracer_provider(), metrics.get_meter_provider(), _logs.get_logger_provider())
providers = {"auto_configure": True, "tracer_provider": TracerProvider(), "meter_provider": MeterProvider(), "logger_provider": LoggerProvider()} if sys.argv[3] == "explicit" else None
app = factory(telemetry=providers)
secret = "otel-private-canary-credential"
@app.get("/_test/{number}")
async def number(number: int):
    return {"number": number}
@app.post("/_body")
async def body(number: int = Body()):
    return {"number": number}
@app.get("/_failure")
async def failure():
    raise RuntimeError(secret)
@app.websocket("/_ws")
async def socket(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_text("ready")
    raise RuntimeError(secret)
for _ in range(2):
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/_test/1?token=" + secret, headers={"Authorization": "Bearer " + secret}).status_code == 200
        assert client.get("/_test/" + secret).status_code == 422
        assert client.get("/_failure").status_code == 500
        assert client.post("/_body", json=secret).status_code == 422
        try:
            with client.websocket_connect("/_ws") as connection:
                assert connection.receive_text() == "ready"
        except RuntimeError as error:
            assert str(error) == secret
if providers is not None:
    assert before == (trace.get_tracer_provider(), metrics.get_meter_provider(), _logs.get_logger_provider())
    with providers["tracer_provider"].get_tracer("ownership").start_as_current_span("caller-provider-alive") as span:
        assert span.is_recording()
    for name in ("tracer_provider", "meter_provider", "logger_provider"):
        assert providers[name].force_flush()
        providers[name].shutdown()
server.shutdown()
server.server_close()
if sys.argv[2] == "disabled":
    assert not payloads, payloads
else:
    decoders = {"/v1/traces": ExportTraceServiceRequest, "/v1/metrics": ExportMetricsServiceRequest, "/v1/logs": ExportLogsServiceRequest}
    assert set(payloads) == set(decoders), payloads.keys()
    assert all(secret.encode() not in payload for batches in payloads.values() for payload in batches)
    spans = []
    logs = []
    for path, decoder in decoders.items():
        for payload in payloads[path]:
            decoded = decoder.FromString(payload)
            assert decoded.ListFields()
            if path == "/v1/traces":
                spans.extend(span for resource in decoded.resource_spans for scope in resource.scope_spans for span in scope.spans)
            if path == "/v1/logs":
                logs.extend(record for resource in decoded.resource_logs for scope in resource.scope_logs for record in scope.log_records)
    assert sum(span.name == "GET /_test/{number}" for span in spans) == 4
    assert sum(span.name == "WS /_ws" for span in spans) == 2
    assert len(logs) == 8
    assert any(attribute.key == "exception.type" for record in logs for attribute in record.attributes)
print("OTLP export contract passed")
"""
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("OTEL_") and key != "FASTAPI_OTEL_AUTO_CONFIGURE"
    }
    for mode in ("disabled", "enabled"):
        result = subprocess.run(
            [sys.executable, str(probe), module, mode, provider_mode],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OTLP export contract passed" in result.stdout

# proxmox-mock-api

Standalone schema-driven FastAPI mock service for the generated Proxmox API.

## Run

```bash
uv run proxmox-mock-api
```

Or with uvicorn:

```bash
uv run uvicorn proxmox_mock.main:app --host 0.0.0.0 --port 8000 --reload
```

## Test

```bash
uv run pytest tests
```

## OpenTelemetry

The mock uses `fastapi[standard]==0.142.2` with native HTTP traces, request metrics, validation/error logs, and WebSocket traces and logs. No collector endpoint is configured by default. Set `OTEL_SERVICE_NAME=proxmox-mock` and an operator-selected `OTEL_EXPORTER_OTLP_ENDPOINT` before startup to opt into OTLP HTTP/protobuf export. Native SDK processors redact concrete request paths, query values, exception messages, and stack traces while retaining route templates and error classification.

`create_mock_app(telemetry={"auto_configure": False})` supports an existing monitoring library that already exports telemetry. Caller-owned providers retain their lifecycle and exporter order; install `proxmox_mock.telemetry.ExceptionPrivacyProcessor` before caller-owned log exporters when redaction is required. See the [configuration guide](../docs/getting-started/configuration.md#opentelemetry) and [FastAPI OpenTelemetry documentation](https://fastapi.tiangolo.com/advanced/opentelemetry/) for provider settings and standard environment variables.

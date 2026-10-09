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

The mock uses `fastapi[standard]==0.143.0` with native HTTP traces, request metrics, validation/error logs, and WebSocket traces and logs. No collector endpoint is configured by default. Set `FASTAPI_OTEL_AUTO_CONFIGURE=true` and an operator-selected `OTEL_EXPORTER_OTLP_ENDPOINT` before startup to opt into native OTLP HTTP/protobuf export. Select `OTEL_SERVICE_NAME` explicitly when a service identity is needed. Endpoint configuration alone does not enable automatic export; an explicit `telemetry={"auto_configure": True}` mapping also opts in, and explicit false overrides the environment. Native SDK processors redact concrete request paths, query values, exception messages, and stack traces while retaining route templates and error classification.

`create_mock_app(telemetry={"auto_configure": False})` supports an existing monitoring library that already exports telemetry. Caller-owned providers retain their lifecycle and exporter order; install `proxmox_mock.telemetry.ExceptionPrivacyProcessor` before caller-owned log exporters when redaction is required. See the [configuration guide](../docs/getting-started/configuration.md#opentelemetry) and [FastAPI OpenTelemetry documentation](https://fastapi.tiangolo.com/advanced/opentelemetry/) for provider settings and standard environment variables.

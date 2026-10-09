# proxmox-mock Package Guide

## Workspace Context

This file lives at `<repository-root>/proxmox-mock/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Standalone `proxmox-mock-api` package — a schema-driven FastAPI mock for the generated Proxmox API. Published separately to PyPI and used as a dev dependency of `proxbox_api` for test isolation. The mock serves the same OpenAPI surface as a real Proxmox node so integration tests do not require a live cluster.

## Package Structure

```
proxmox-mock/
├── proxmox_mock/          # Python package
│   ├── app.py             # FastAPI app factory (create_mock_app)
│   ├── main.py            # Standalone entry point and CLI runner
│   ├── routes.py          # All mock route implementations
│   ├── openapi.py         # OpenAPI schema generation for the mock
│   ├── state.py           # In-memory mock state (nodes, VMs, storage)
│   ├── schema_helpers.py  # Schema validation and response shaping helpers
│   ├── errors.py          # Mock-specific exceptions
│   ├── log.py             # Logging setup for the mock app
│   ├── codegen/           # Pydantic model generation from OpenAPI
│   │   ├── pydantic_generator.py
│   │   └── utils.py
│   └── generated/         # Bundled openapi.json schema snapshot
├── tests/                 # pytest test suite for the mock package
│   └── test_generated_proxmox_mock_routes.py
├── pyproject.toml         # Standalone package config (name: proxmox-mock-api)
├── uv.lock                # Locked deps for the mock package only
└── Dockerfile             # Container image for the mock server
```

## Key Files

| File | Role |
|------|------|
| `proxmox_mock/app.py` | `create_mock_app()` — entrypoint for embedding the mock in tests |
| `proxmox_mock/routes.py` | All route handlers, registered at app creation time |
| `proxmox_mock/state.py` | In-memory mutable state; reset between test runs via `reset_state()` |
| `proxmox_mock/schema_helpers.py` | Validates requests and shapes Proxmox-style responses |
| `proxmox_mock/generated/openapi.json` | Bundled schema snapshot — do not edit by hand |

## Usage in proxbox_api Tests

```python
from proxmox_mock.app import create_mock_app
from httpx import AsyncClient, ASGITransport

@pytest.fixture
async def mock_proxmox():
    app = create_mock_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://mock") as client:
        yield client
```

## Entry Points

- Import: `from proxmox_mock.app import create_mock_app`
- Standalone server: `python -m proxmox_mock.main` or `uvicorn proxmox_mock.main:app`
- Docker: `docker run emersonfelipesp/proxmox-mock-api`

## Development Checks

Run these from inside the `proxmox-mock/` directory:

```bash
uv run ruff check .
uv run ruff format --check .
uv run python -m compileall proxmox_mock tests
uv run python -c "import proxmox_mock.main"
uv run pytest tests
```

## Extension Rules

- Add new mock routes to `routes.py` and matching state to `state.py`.
- Keep `generated/openapi.json` in sync with the Proxmox schema version it targets.
- Do not import from `proxbox_api` — this package must remain standalone.
- Reset in-memory state between test cases to avoid cross-test pollution.

## Native OpenTelemetry

The backend, Firecracker host agent, and standalone Proxmox mock use `fastapi[standard]==0.143.0` and native FastAPI telemetry. Public defaults never select a collector endpoint. Operators opt into native automatic OTLP HTTP/protobuf export with `FASTAPI_OTEL_AUTO_CONFIGURE=true` and a user-selected `OTEL_EXPORTER_OTLP_ENDPOINT` before lifespan startup, or with an explicit `telemetry={"auto_configure": True}` mapping. Endpoint configuration alone does not enable automatic export. Explicit `auto_configure=False` overrides the environment opt-in. Application factories accept keyword-only `telemetry` settings and explicit providers; set `auto_configure=False` if another library already owns environment export, and retain caller ownership of explicit provider shutdown. Do not add duplicate FastAPI/ASGI instrumentation. Preserve HTTP authentication, SSE, WebSocket admission, console relay, and lifecycle contracts when upgrading dependencies. Configuration and sensitive-error-log guidance are documented in both language versions of `docs/getting-started/configuration.md`.

`OTEL_SDK_DISABLED=true` is applied to a copy before native construction and disables automatic configuration, traces, metrics, and logs without replacing selected providers. Selected SDK trace and log providers still receive one privacy processor each. This registration does not activate request telemetry. Span sanitation occurs at span start; log sanitation follows registration order and is not retroactive. Keep this package's helper independent of `proxbox_api`.

Native exporter privacy processors redact concrete HTTP paths and query values and remove arbitrary exception messages and stack traces while retaining route templates and error classification. Install caller-owned log redaction before caller-owned exporters; existing exporter order is preserved. Keep the standalone mock privacy helper independent of `proxbox_api`.

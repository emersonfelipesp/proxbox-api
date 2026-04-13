# Testing

## Test stack

- `pytest`
- `httpx`
- FastAPI `TestClient`
- `proxmox-sdk` (mock testing)

Test dependencies are defined in `pyproject.toml` under
`[project.optional-dependencies] -> test`.

Install them with:

```bash
uv sync --extra test --group dev
```

## Run tests

```bash
pytest
```

For the schema-generation and typed helper path specifically:

```bash
pytest tests/test_pydantic_generator_models.py tests/test_session_and_helpers.py
```

For the current route and docs contract surface:

```bash
pytest tests/test_generated_proxmox_routes.py tests/test_proxmox_codegen_docs.py tests/test_api_routes.py tests/test_stub_routes.py tests/test_admin_logs.py
```

## Proxmox Mock Testing

All tests use `proxmox-sdk` mock features to validate Proxmox API integration. The test suite supports three mock modes:

### Three Mock Modes

| Mode | Fixture | Port | Speed | Use Case |
|------|---------|------|-------|----------|
| **MockBackend** | `proxmox_mock_backend` | N/A | Fastest | Development iteration |
| **HTTP Published** | `proxmox_mock_http_published` | 8006 | Fast | User experience validation |
| **HTTP Local** | `proxmox_mock_http_local` | 8007 | Fast | Pre-release validation |

### 1. In-process MockBackend (`@pytest.mark.mock_backend`)

The fastest mode using the `proxmox_sdk.sdk.backends.mock.MockBackend`. No HTTP server required.

```python
@pytest.mark.mock_backend
async def test_vm_sync(proxmox_mock_backend):
    """Fast test using in-process MockBackend."""
    vms = await proxmox_mock_backend.request("GET", "/api2/json/nodes/pve01/qemu")
```

### 2. HTTP Published Container (`@pytest.mark.mock_http`)

Uses the published Docker image `emersonfelipesp/proxmox-sdk:latest` on port 8006.

```python
@pytest.mark.mock_http
async def test_vm_sync_http(proxmox_mock_http_published):
    """Realistic test using HTTP container (published image)."""
    vms = await proxmox_mock_http_published.nodes.get()
```

### 3. HTTP Local Build Container (`@pytest.mark.mock_http`)

Uses a locally-built Docker image from `./proxmox-sdk` on port 8007.

```python
@pytest.mark.mock_http
async def test_vm_sync_http_local(proxmox_mock_http_local):
    """Realistic test using HTTP container (local build)."""
    vms = await proxmox_mock_http_local.nodes.get()
```

### Running Tests with Docker Containers

Start both mock containers:

```bash
# Using docker compose (v2)
docker compose up -d

# Or using docker-compose (v1)
docker-compose up -d
```

Run the full test suite with orchestration script:

```bash
./scripts/test-with-mock.sh
```

Run specific test modes:

```bash
# MockBackend only (fast, no HTTP)
PROXMOX_API_MODE=mock uv run pytest tests --ignore=tests/e2e -m "mock_backend"

# HTTP published only
PROXMOX_API_MODE=mock PROXMOX_MOCK_PUBLISHED_URL=http://localhost:8006 \
  uv run pytest tests --ignore=tests/e2e -m "mock_http"

# HTTP local only
PROXMOX_API_MODE=mock PROXMOX_MOCK_LOCAL_URL=http://localhost:8007 \
  uv run pytest tests --ignore=tests/e2e -m "mock_http"
```

### Dual Mode Testing

Tests can run against both MockBackend AND HTTP containers:

```python
@pytest.mark.mock_backend
@pytest.mark.mock_http
async def test_vm_sync_all_modes(request, proxmox_mock_backend,
                                   proxmox_mock_http_published,
                                   proxmox_mock_http_local):
    """Test runs 3 times: backend, published HTTP, and local HTTP."""
    ...
```

### Docker Compose Services

The `docker-compose.yml` defines two services:

```yaml
services:
  proxmox-mock-published:
    image: emersonfelipesp/proxmox-sdk:latest
    ports:
      - "8006:8000"
    environment:
      - PROXMOX_API_MODE=mock
      - PROXMOX_MOCK_SCHEMA_VERSION=latest

  proxmox-mock-local:
    build:
      context: ./proxmox-sdk
      dockerfile: Dockerfile
    ports:
      - "8007:8000"
    environment:
      - PROXMOX_API_MODE=mock
      - PROXMOX_MOCK_SCHEMA_VERSION=latest
```

Both services:
- Use `PROXMOX_API_MODE=mock`
- Set `PROXMOX_MOCK_SCHEMA_VERSION=latest`
- Have health checks enabled
- Have no persistent volumes (fresh state on restart)

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXMOX_API_MODE` | `real` | Set to `mock` for test mode |
| `PROXMOX_MOCK_PUBLISHED_URL` | `http://localhost:8006` | Published container URL |
| `PROXMOX_MOCK_LOCAL_URL` | `http://localhost:8007` | Local container URL |
| `PYTEST_CURRENT_TEST` | auto-set | Auto-detects pytest environment |

### pytest Markers

| Marker | Description |
|--------|-------------|
| `mock_backend` | Tests using in-process MockBackend (fast) |
| `mock_http` | Tests using HTTP mock container |
| `image_http` | Smoke tests against published Docker images |

## Targeted endpoint tests

The file `tests/test_endpoint_crud.py` includes dedicated coverage for:

- Proxmox endpoint CRUD lifecycle.
- Proxmox endpoint auth validation rules.
- NetBox endpoint singleton enforcement.

Run only this test file:

```bash
pytest tests/test_endpoint_crud.py
```

## Compile check

```bash
python -m compileall proxbox_api
```

## Recommended pre-PR checks

- `pytest`
- `python -m compileall proxbox_api`
- `mkdocs build --strict` (when docs changed)

## Generated Proxmox contract checks

When changing `proxbox_api/proxmox_codegen/` or the sync-facing Proxmox service layer:

- Regenerate `proxbox_api/generated/proxmox/*/pydantic_models.py` from the checked-in `openapi.json` artifacts.
- Confirm array-of-object responses still emit concrete `...ResponseItem` schemas.
- Confirm helper-backed routes still return payloads compatible with existing sync code.

## Troubleshooting

### Container Won't Start

```bash
# Check container logs
docker compose logs proxmox-mock-published
docker compose logs proxmox-mock-local

# Verify health status
docker compose ps

# Restart containers
docker compose restart
```

### Tests Timeout

```bash
# Increase health check timeout in docker-compose.yml
# Or wait longer before running tests:
sleep 30
```

### Import Errors

```bash
# Ensure dependencies are installed
uv sync --frozen --extra test --group dev

# Verify imports
uv run python -c "from proxbox_api.testing import MockProxmoxContext"
```

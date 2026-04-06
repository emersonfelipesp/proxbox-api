# Testing with Proxmox Mock API

## Overview

All tests in `proxbox-api` use `proxmox-openapi` mock features to validate the integration between Proxmox API and NetBox. The testing strategy supports three mock modes:

1. **In-process MockBackend** (fastest) - No HTTP server required
2. **HTTP container (published)** - Uses Docker Hub image at `localhost:8006`
3. **HTTP container (local)** - Uses locally-built image at `localhost:8007`

## Three Mock Modes

### 1. In-process MockBackend (`@pytest.mark.mock_backend`)

The fastest mode using the `proxmox_openapi.sdk.backends.mock.MockBackend`. No HTTP server is started.

**Usage:**
```python
@pytest.mark.mock_backend
async def test_vm_sync(proxmox_mock_backend):
    """Fast test using in-process MockBackend."""
    vms = await proxmox_mock_backend.request("GET", "/api2/json/nodes/pve01/qemu")
```

**Fixture:** `proxmox_mock_backend` in `tests/conftest.py`

### 2. HTTP Published Container (`@pytest.mark.mock_http`)

Uses the published Docker image `emersonfelipesp/proxmox-openapi:latest` on port 8006.

**Usage:**
```python
@pytest.mark.mock_http
async def test_vm_sync_http(proxmox_mock_http_published):
    """Realistic test using HTTP container (published image)."""
    vms = await proxmox_mock_http_published.nodes.get()
```

**Fixture:** `proxmox_mock_http_published` in `tests/conftest.py`

### 3. HTTP Local Build Container (`@pytest.mark.mock_http`)

Uses a locally-built Docker image from `./proxmox-openapi` on port 8007.

**Usage:**
```python
@pytest.mark.mock_http
async def test_vm_sync_http_local(proxmox_mock_http_local):
    """Realistic test using HTTP container (local build)."""
    vms = await proxmox_mock_http_local.nodes.get()
```

**Fixture:** `proxmox_mock_http_local` in `tests/conftest.py`

## Running Tests

### All Tests (Both Mock Containers Required)

```bash
# Start both containers
docker-compose up -d

# Wait for health checks
for i in {1..30}; do
  if docker-compose ps | grep -q "healthy"; then break; fi
  sleep 2
done

# Run full test suite with both containers
./scripts/test-with-mock.sh

# Cleanup
docker-compose down
```

### Unit/Integration Tests Only

```bash
# MockBackend tests (fast, no HTTP)
PROXMOX_API_MODE=mock uv run pytest tests --ignore=tests/e2e -m "mock_backend and not mock_http"

# HTTP published tests
PROXMOX_API_MODE=mock PROXMOX_MOCK_PUBLISHED_URL=http://localhost:8006 \
  uv run pytest tests --ignore=tests/e2e -m "mock_http"

# HTTP local tests
PROXMOX_API_MODE=mock PROXMOX_MOCK_LOCAL_URL=http://localhost:8007 \
  uv run pytest tests --ignore=tests/e2e -m "mock_http"
```

### E2E Tests Only

```bash
# With containers running (from docker-compose)
uv run pytest tests/e2e -m "not image_http"

# Against published container
PROXMOX_MOCK_HTTP_URL=http://localhost:8006 \
  uv run pytest tests/e2e -m "mock_http"
```

## Dual Mode Testing

Tests can be marked to run against both MockBackend AND HTTP containers:

```python
@pytest.mark.mock_backend
@pytest.mark.mock_http
async def test_vm_sync_all_modes(request, proxmox_mock_backend, 
                                   proxmox_mock_http_published,
                                   proxmox_mock_http_local):
    """Test runs 3 times: backend, published HTTP, and local HTTP."""
    if request.param == "backend":
        # Test with MockBackend
        pass
    elif request.param == "http_published":
        # Test with published container
        pass
    else:
        # Test with local container
        pass
```

## Why Three Mock Modes?

- **MockBackend:** Fast execution for rapid test iteration during development
- **HTTP Published:** Validates against the stable release that users install
- **HTTP Local:** Catches integration issues with the latest code before publishing

Both HTTP containers run in isolated networks with no shared state, ensuring test isolation.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXMOX_API_MODE` | `real` | Set to `mock` for test mode |
| `PROXMOX_MOCK_PUBLISHED_URL` | `http://localhost:8006` | Published container URL |
| `PROXMOX_MOCK_LOCAL_URL` | `http://localhost:8007` | Local container URL |
| `PYTEST_CURRENT_TEST` | auto-set | Auto-detects pytest environment |

## Docker Compose Services

The `docker-compose.yml` defines two services:

```yaml
services:
  proxmox-mock-published:
    image: emersonfelipesp/proxmox-openapi:latest
    ports:
      - "8006:8000"
    
  proxmox-mock-local:
    build:
      context: ./proxmox-openapi
      dockerfile: Dockerfile
    ports:
      - "8007:8000"
```

Both services:
- Use `PROXMOX_API_MODE=mock`
- Set `PROXMOX_MOCK_SCHEMA_VERSION=latest`
- Have health checks enabled
- Have no persistent volumes (fresh state on restart)

## CI Execution Order

In GitHub Actions CI:

1. **MockBackend tests** (fast fail) - Runs first
2. **Build local image** - Parallel with step 1
3. **HTTP published tests** - Validates stable release
4. **HTTP local tests** - Validates latest code

This ensures fast feedback while validating against both the published image and the latest local build.

## Test Markers

| Marker | Description |
|--------|-------------|
| `mock_backend` | Tests using in-process MockBackend |
| `mock_http` | Tests using HTTP mock container |
| `image_http` | Smoke tests against published Docker images |

## Troubleshooting

### Container Won't Start

```bash
# Check container logs
docker-compose logs proxmox-mock-published
docker-compose logs proxmox-mock-local

# Verify health status
docker-compose ps

# Restart containers
docker-compose restart
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

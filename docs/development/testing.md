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

## netbox-proxbox REST contract

`tests/test_plugin_rest_contract.py` parses `proxbox_api/` with Python's `ast`
module and checks plugin route paths and query keys against
`tests/fixtures/netbox_proxbox_0_0_32_post1_contract.json`. The fixture comes
from `manage.py spectacular` inside the published netbox-proxbox testing
appliance, after a real NetBox database migration. It is not derived from the
plugin's Python source because NetBox base filter sets and lookup suffixes are
assembled at runtime.

Regenerate the fixture for every paired netbox-proxbox release:

```bash
docker exec <running-appliance> /bin/sh -c \
  'cd /opt/netbox/netbox && python manage.py spectacular --format openapi-json --file /var/tmp/netbox-openapi.json --skip-checks'
docker cp <running-appliance>:/var/tmp/netbox-openapi.json .cache/netbox-openapi.json
uv run python scripts/generate_plugin_contract_fixture.py \
  .cache/netbox-openapi.json \
  tests/fixtures/netbox_proxbox_<version>_contract.json \
  --plugin-version <version> \
  --plugin-wheel-sha256 <sha256> \
  --image <repository:tag> \
  --image-digest <sha256:digest> \
  --netbox-version <version> \
  --generation-command <recorded-command>
```

The extractor uses an explicit contract table for every supported
`netbox_rest` helper. The table expands collection and detail operations: for
example, reconcile helpers require collection GET and POST plus detail PATCH;
bulk delete requires both collection and detail DELETE because the runtime
choice depends on the deduplicated batch size; and create requires its
collection recovery GET only when a lookup is supplied. Unknown helpers fail
closed.
The fixture records every served method for each plugin path, so a call must
match an exact path and every operation the helper can issue.
There is no prefix fallback: an action or sub-path missing from the fixture
fails. Capability probes (`rest_probe_async`) are method-agnostic because they
deliberately GET routes that may be POST-only. Filter and control keys are
checked against the path's GET parameters.

The OpenAPI evidence for NetBox 4.7.0 includes `limit`, `offset`, `ordering`,
`brief`, and `fields` where the route supports them. It exposes `omit`, not
`exclude`. The test therefore grants no blanket pagination or projection-key
exception: every extracted control key must appear on that exact route in the
fixture.

The test accounts for every `/api/plugins/proxbox/` string literal under
`proxbox_api/`, including literals passed through local variables and simple URL
builders. Executable urllib and E2E cleanup paths emit checked GET and DELETE
operations. Remaining literal exceptions are split into non-executable
documentation/model strings and route constants. Every route-constant exception
is bound to specific emitted `(file, function, route, method)` operations; an
unconsumed or stale entry fails.

The extractor statically recognizes a maintained set of project-local wrappers.
It substitutes concrete caller paths and lookup keys, applies declared remaps
and parent-field derivations, and independently collects literal query keys from
the real wrapper bodies and their declared forwarding chain, including keys a
wrapper adds to a local query dictionary by subscript assignment, `update`,
`setdefault`, or `|=`; a key that cannot be resolved to a string literal fails.
It does not execute
Python or prove that a runtime branch is taken. Unresolved paths, helpers, query
expressions, or dictionary unpacking fail unless an exact dynamic exception is
present; there are currently no dynamic exceptions. The original route/filter
floor from commit `127df044` remains, and a generated minimum set of
`(file, function, route, method, filter)` contracts prevents one call site from
masking lost coverage at another site.

## Coverage gate

The required non-E2E core suite measures branch coverage for `proxbox_api` and
enforces a 65.40% ratchet from a 65.51% measured baseline. The 85% coverage
level is the long-term target, not the current gate.

```bash
uv run pytest tests/ -n auto \
  --ignore=tests/e2e \
  --ignore=tests/test_generated_proxmox_routes.py \
  --cov=proxbox_api \
  --cov-branch \
  --cov-report=term-missing \
  --cov-report=xml:coverage.xml
```

The Gitea pre-merge gate (`.gitea/workflows/ci.yml`) runs the same suite with
branch coverage retained by `branch = true` in `pyproject.toml`. It requests
`COVERAGE_CORE=sysmon`, but Python 3.12 cannot measure branches with that core,
so coverage.py falls back to its default core. The gate pins `-n 4 --dist
worksteal` to leave CPU headroom for telemetry subprocesses within the runner's
8-CPU quota. This scheduling change preserves the coverage threshold, test
selection, assertions, and subprocess deadlines.

The TestPyPI and PyPI candidate validation jobs run the same mocked suite with
two pytest-xdist workers and `--dist loadgroup`, preserving the singleton-state
test groups. Python 3.13 retains branch coverage, an explicit `coverage.xml`
artifact for 14 days, and `--durations=20`, but omits the large terminal
missing-lines report from release logs. Coverage's `sysmon` core cannot measure
branches on Python 3.13, so these jobs intentionally use the default coverage
core instead of requesting `COVERAGE_CORE=sysmon` and silently falling back.
Python 3.12 runs the same compatibility suite without coverage, so both
supported interpreters are validated without collecting the metric twice.

Generated schema output (`proxbox_api/generated/`) and E2E support code
(`proxbox_api/e2e/`) are excluded from the core metric. The E2E support package
is covered by the separate Docker E2E matrix, while generated declarations would
inflate the metric without testing generated behavior.

## Rust reconciliation tests

The optional Rust reconciliation package lives in `proxbox-reconcile-rs/`.
Python remains the default engine, so the normal test suite must pass without the native package.

Run the Python fixture contracts and skip Rust parity when the native package is absent:

```bash
uv run pytest tests/reconciliation
```

Run Rust unit tests without the PyO3 extension-module link mode:

```bash
cd proxbox-reconcile-rs
cargo test --no-default-features
```

Install the local native package into the root `uv` environment:

```bash
uv pip install -e proxbox-reconcile-rs
```

Run strict compare-mode parity:

```bash
PROXBOX_TEST_RECONCILIATION_ENGINE=compare \
PROXBOX_TEST_RECONCILIATION_COMPARE_STRICT=true \
uv run pytest tests/reconciliation
```

The `PROXBOX_TEST_*` variables are consumed only by the reconciliation test
fixture and are translated into plugin-setting values. Production runtime
continues to accept engine selection only from NetBox plugin settings.

Run explicit Rust-engine tests:

```bash
PROXBOX_TEST_RECONCILIATION_ENGINE=rust uv run pytest tests/reconciliation
```

Run the synthetic benchmark harness:

```bash
uv run python benchmarks/reconciliation/bench_vm_queue.py --sizes 100 1000 10000
uv run python benchmarks/reconciliation/bench_vm_queue.py --sizes 10000 --pathological
```

The harness preserves fixture `sync_state_fields`, so endpoint-first identity
matches the parity-test path. Output records the exact commit (including a dirty
marker), Python/platform and package versions, measured paths, repeat count,
wall time, process CPU time, and traced Python peak memory. These are synthetic
planner measurements, not production sync claims. Measure the complete sync in
staging with the detailed batch timing log plus SDK request-count/latency,
event-loop responsiveness, retry/failure, and recovery evidence before changing
cache, executor, or concurrency policy.

If compare mode reports mismatches, keep `reconciliation_engine=python` in the
NetBox plugin settings and inspect `proxbox_reconcile_mismatch_total` through `/cache/metrics` or
`/cache/metrics/prometheus`.

For the v0.0.11 surface specifically (HA routes, operational verbs, the
`allow_writes` gate, and supporting helpers):

```bash
pytest \
  tests/test_proxmox_ha_routes.py \
  tests/test_proxmox_actions_gate.py \
  tests/test_proxmox_actions_start.py \
  tests/test_proxmox_actions_stop.py \
  tests/test_proxmox_actions_snapshot.py \
  tests/test_proxmox_actions_migrate_verb.py \
  tests/test_verb_dispatch_helpers.py \
  tests/test_sync_active.py \
  tests/test_name_collision.py \
  tests/test_vm_cloudinit_mapping.py \
  tests/test_description_metadata.py
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

`unit` and `integration` are directory conventions, not pytest markers, so do not pass `-m unit` / `-m integration`.

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
- `cargo test --no-default-features` in `proxbox-reconcile-rs` (when Rust changes)

## Generated Proxmox contract checks

When changing `proxbox_api/proxmox_codegen/` or the sync-facing Proxmox service layer:

- Regenerate `proxbox_api/generated/proxmox/*/pydantic_models.py` from the checked-in `openapi.json` artifacts.
- Confirm array-of-object responses still emit concrete `...ResponseItem` schemas.
- Confirm helper-backed routes still return payloads compatible with existing sync code.

The backend-local generated artifacts support the proxy and viewer surfaces;
they are not the runtime response-model authority for synchronization. Sync
helpers validate Proxmox responses with the generated models from the exactly
pinned `proxmox-sdk` version in `pyproject.toml` and `uv.lock`. Update that pin
and lock together, then run `tests/test_proxmox_sdk_model_authority.py` plus the
VM synchronization suites whenever the SDK response contract changes.

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

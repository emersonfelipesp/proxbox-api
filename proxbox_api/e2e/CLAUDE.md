# proxbox_api/e2e Directory Guide

## Purpose

Playwright-based helpers for NetBox demo authentication, shared e2e test data, and mock Proxmox fixtures.

## Current Files

- `demo_auth.py`: Async demo.netbox.dev login and token provisioning with explicit browser and dependency errors.
- `session.py`: Session bootstrap helpers for e2e tests and the shared `proxbox e2e testing` tag.
- `fixtures/proxmox_mock.py`: Mock Proxmox clusters, nodes, VMs, and storage fixtures.
- `fixtures/test_data.py`: Reusable constants, env-driven demo config, and unique resource helpers.
- `fixtures/__init__.py`: Fixture package namespace.
- `tests/e2e/`: Repository test suite that consumes these helpers.

## Running Tests

```bash
# Install Playwright browsers the first time
playwright install chromium

# Run e2e tests with parallel execution
pytest tests/e2e/ -n auto

# Run a specific auth smoke test
pytest tests/e2e/test_demo_auth.py -v
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXBOX_E2E_DEMO_URL` | `https://demo.netbox.dev` | NetBox demo URL |
| `PROXBOX_E2E_TIMEOUT` | `60` | Timeout in seconds |
| `PROXBOX_E2E_HEADLESS` | `true` | Run browser headless |
| `PROXBOX_E2E_USERNAME` | generated | Optional shared demo username |
| `PROXBOX_E2E_PASSWORD` | generated | Optional shared demo password |

## E2E Tag

All synced objects are tagged with `proxbox e2e testing` (color `4caf50`, slug `proxbox-e2e-testing`).

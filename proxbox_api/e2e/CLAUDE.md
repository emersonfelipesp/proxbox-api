# proxbox_api/e2e Directory Guide

## Purpose

Playwright-based helpers for NetBox demo authentication, shared e2e test data, and mock Proxmox fixtures.

## Current Files

- `demo_auth.py`: async demo.netbox.dev login and token provisioning with explicit browser and dependency errors.
- `session.py`: session bootstrap helpers for e2e tests and the shared `proxbox e2e testing` tag.
- `fixtures/proxmox_mock.py`: mock Proxmox clusters, nodes, VMs, and storage fixtures.
- `fixtures/test_data.py`: reusable constants, env-driven demo config, and unique resource helpers.
- `fixtures/__init__.py`: fixture package namespace.

## How These Helpers Are Used

- The repository e2e suite under `tests/e2e/` imports these helpers directly.
- Browser-backed auth flows rely on the Playwright session helpers here rather than duplicating login code in tests.
- Mock Proxmox fixtures keep the test suite deterministic when it does not need a live cluster.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `PROXBOX_E2E_DEMO_URL` | `https://demo.netbox.dev` | NetBox demo URL |
| `PROXBOX_E2E_TIMEOUT` | `60` | Timeout in seconds |
| `PROXBOX_E2E_HEADLESS` | `true` | Run the browser headless |
| `PROXBOX_E2E_USERNAME` | generated | Optional shared demo username |
| `PROXBOX_E2E_PASSWORD` | generated | Optional shared demo password |

## Verification

```bash
playwright install chromium
pytest tests/e2e/ -n auto
pytest tests/e2e/test_demo_auth.py -v
```

## Extension Guidance

- Keep e2e-specific state and tags isolated in this package.
- Reuse these fixtures from tests instead of duplicating setup logic.
- If a helper becomes part of production code, move it into the appropriate runtime package and leave only test support here.

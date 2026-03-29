# E2E Testing Module

End-to-end tests using Playwright to authenticate with NetBox demo and sync mock Proxmox data.

## Overview

- **Authentication**: Async Playwright auth for NetBox demo (`demo_auth.py`)
- **Session**: `DemoSessionBuilder` for creating authenticated sessions with e2e tag
- **Fixtures**: Mock Proxmox API data for testing sync logic
- **Tests**: Device, VM, and backup sync e2e tests

## Key Files

- `proxbox_api/e2e/demo_auth.py` - Playwright auth flow
- `proxbox_api/e2e/session.py` - Session builder and tag management
- `proxbox_api/e2e/fixtures/proxmox_mock.py` - Mock Proxmox classes
- `proxbox_api/e2e/fixtures/test_data.py` - Test data constants
- `tests/e2e/` - E2E test suite

## Running Tests

```bash
# Install Playwright browsers (first time)
playwright install chromium

# Run e2e tests with parallel execution
pytest tests/e2e/ -n auto

# Run specific test
pytest tests/e2e/test_demo_auth.py -v
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXBOX_E2E_DEMO_URL` | `https://demo.netbox.dev` | NetBox demo URL |
| `PROXBOX_E2E_TIMEOUT` | `60` | Timeout in seconds |
| `PROXBOX_E2E_HEADLESS` | `true` | Run browser headless |

## E2E Tag

All synced objects are tagged with `proxbox e2e testing` (color: `4caf50`, slug: `proxbox-e2e-testing`).

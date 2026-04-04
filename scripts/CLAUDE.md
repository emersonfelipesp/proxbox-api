# scripts/ Directory Guide

## Purpose

Utility and maintenance scripts for the `proxbox-api` project. These are one-off or periodic scripts run by developers or CI, not part of the application runtime.

## Files

| File | Role |
|------|------|
| `refresh_schemas.py` | Regenerates the Proxmox and NetBox OpenAPI schema snapshots in `proxbox_api/generated/`. Run this when a new Proxmox or NetBox version is targeted. |

## Running

```bash
uv run python scripts/refresh_schemas.py
```

After running, review diffs in `proxbox_api/generated/` before committing. The nightly schema refresh CI job (`.github/workflows/nightly-schema-refresh.yml`) runs this automatically.

## Adding New Scripts

- Name scripts descriptively: `<verb>_<noun>.py` (e.g., `seed_test_data.py`).
- Keep scripts standalone — they should be runnable with `uv run python scripts/<name>.py` without additional setup.
- Add an entry to this file when adding a new script.

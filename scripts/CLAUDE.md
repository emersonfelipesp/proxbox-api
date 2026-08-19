# scripts/ Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/scripts/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Utility and maintenance scripts for the `proxbox-api` project. These are one-off or periodic scripts run by developers or CI, not part of the application runtime.

## Files

| File | Role |
|------|------|
| `refresh_schemas.py` | Regenerates the Proxmox and NetBox OpenAPI schema snapshots in `proxbox_api/generated/`. Run this when a new Proxmox or NetBox version is targeted. |
| `prepare_offline_release.py` | Converts the reviewed `Dockerfile.release` plus a CI-populated wheelhouse into the canonical schema-2 offline context embedded only in release sdists. |
| `verify_offline_release_sdist.py` | Streams a bounded release sdist into a new context, rehashes its exact offline wheelhouse/lock, and permits only the two literal pinned base images plus declared-stage `COPY --from` sources before the network-disabled CI Docker build. The release gate separately binds that required GitHub job to the reviewed source-SHA workflow bytes. |

## Running

```bash
uv run python scripts/refresh_schemas.py
```

After running, review diffs in `proxbox_api/generated/` before committing. The nightly schema refresh CI job (`.github/workflows/nightly-schema-refresh.yml`) runs this automatically.

## Adding New Scripts

- Name scripts descriptively: `<verb>_<noun>.py` (e.g., `seed_test_data.py`).
- Keep scripts standalone — they should be runnable with `uv run python scripts/<name>.py` without additional setup.
- Add an entry to this file when adding a new script.

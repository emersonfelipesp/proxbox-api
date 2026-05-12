# proxbox_api/routes/sync Directory Guide

## Purpose

Internal sync helper routes that expose targeted, per-object synchronization operations. These routes complement the full-update orchestration in `app/full_update.py` by providing fine-grained control over individual object syncs with dry-run and dependency-creation options.

## Structure

```
routes/sync/
├── __init__.py          # Router registration and shared sync route helpers
├── active.py            # GET /sync/active soft-probe endpoint (issue #71)
└── individual/          # Per-object sync route handlers (see individual/CLAUDE.md)
```

The `active.py` route reads the process-local registry maintained by
`proxbox_api.app.sync_state` (registered by both `/full-update` handlers).
It is intentionally a soft probe — a cron / single-exec caller can use it to
fast-fail when a sync is already running on the local replica, but operators
running multiple uvicorn workers should not rely on it as a distributed lock.

## Key Patterns

- Routes here delegate immediately to `proxbox_api/services/sync/individual/` — no business logic lives in the route layer.
- All individual-sync endpoints accept a `dry_run: bool = False` query parameter.
- Responses are structured sync-result payloads, not raw NetBox objects.
- SSE variants emit progress events for long-running syncs.

## Related Guides

- `../../services/sync/individual/CLAUDE.md` — the service implementations behind these routes
- `../virtualization/virtual_machines/CLAUDE.md` — VM-specific sync routes

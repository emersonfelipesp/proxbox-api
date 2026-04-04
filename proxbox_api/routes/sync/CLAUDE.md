# proxbox_api/routes/sync Directory Guide

## Purpose

Internal sync helper routes that expose targeted, per-object synchronization operations. These routes complement the full-update orchestration in `app/full_update.py` by providing fine-grained control over individual object syncs with dry-run and dependency-creation options.

## Structure

```
routes/sync/
├── __init__.py          # Router registration and shared sync route helpers
└── individual/          # Per-object sync route handlers (see individual/CLAUDE.md)
```

## Key Patterns

- Routes here delegate immediately to `proxbox_api/services/sync/individual/` — no business logic lives in the route layer.
- All individual-sync endpoints accept a `dry_run: bool = False` query parameter.
- Responses are structured sync-result payloads, not raw NetBox objects.
- SSE variants emit progress events for long-running syncs.

## Related Guides

- `proxbox_api/services/sync/individual/CLAUDE.md` — the service implementations behind these routes
- `proxbox_api/routes/virtualization/virtual_machines/CLAUDE.md` — VM-specific sync routes

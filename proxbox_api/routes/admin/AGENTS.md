# proxbox_api/routes/admin Module Guide

## Workspace Context

This file lives at `<repository-root>/proxbox_api/routes/admin/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

HTML admin dashboard and backend log buffer routes.

## Current Files

- `__init__.py`: single FastAPI route that renders `templates/admin/index.html`; also includes the `logs` and `encryption` sub-routers.
- `logs.py`: JSON API route for the in-memory backend log buffer.
- `encryption.py`: runtime encryption-key management endpoints.

## Current Behavior

- `GET /admin/` is excluded from OpenAPI and renders the dashboard with `GetNetBoxEndpoint` data.
- `GET /admin/logs` returns log-buffer entries with filters for level, pagination, timestamp, and operation ID.
- `GET /admin/encryption/status` reports whether a credential encryption key is configured and its source (`env`/`plugin`/`local`).
- `POST /admin/encryption/key` persists a caller-supplied key to the local key file (mode 0600) and resets the in-process Fernet cache.
- `POST /admin/encryption/generate` generates a fresh Fernet key, persists it locally, and returns it once.
- `POST /admin/encryption/key`, `/admin/encryption/generate`, and `DELETE /admin/encryption/key` run the complete serialized SQLite replacement protocol in a worker thread, so `BEGIN IMMEDIATE` lock waits never block the event loop. Lock contention returns a secret-free HTTP 503 with `Retry-After`; encrypted stored credentials still return HTTP 409.
- Every persistent credential writer reads the generation and local-file identity under `no_autoflush` before mutating an ORM secret field, encrypts through that captured permit, and conditionally fences the same generation in the ciphertext commit. A generation or file-identity change returns a secret-free, retryable HTTP 409 and rolls back the credential row. Local key publication fsyncs the file, performs the atomic rename, and fsyncs the parent directory before the durable pending fence can be cleared.
- Replacement publishes a durable pending fence before changing the file. A filesystem or final database failure leaves that fence set, and lock contention on a retry must never clear an inherited fence. Only successful replacement clears it, so credential writes fail closed with HTTP 409 until completion. Deletion must fsync the existing key-file directory even when a prior attempt already unlinked the key.
- NetBox endpoint creation and credential changes enforce provenance on the actual material returned to the encryption sink and return HTTP 409 if it is `plugin`, including when plugin authority recovers during the request. Before an authenticated endpoint credential change, configure `PROXBOX_ENCRYPTION_KEY` or an independent local key file and confirm that `/admin/encryption/status` reports `env` or `local`. Unchanged endpoint pushes remain accepted.
- The dashboard uses the shared `proxbox_api.templates` Jinja2 environment.
- The routes are mounted by `proxbox_api.app.factory.create_app()` under the `/admin` prefix.

## How This Route Should Behave

- Keep the dashboard read-only and template-focused.
- Keep persistence, validation, and endpoint CRUD in the NetBox route and service layers.
- Mask secrets if you expose more endpoint fields in the template.
- Preserve off-loop serialization, the durable pending fence, file and directory fsync ordering, actual-source enforcement, and the writer-side generation/file-identity permit as one coordination protocol.

## Extension Guidance

- Keep this module template-only for the dashboard and log-view-only for the API route.
- Add backend logic elsewhere and let this route stay a rendering layer.

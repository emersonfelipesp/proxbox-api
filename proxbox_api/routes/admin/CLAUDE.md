# proxbox_api/routes/admin Module Guide

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
- `DELETE /admin/encryption/key` clears the local key file; returns 409 if any `enc:`-prefixed value still exists in the database.
- The dashboard uses the shared `proxbox_api.templates` Jinja2 environment.
- The routes are mounted by `proxbox_api.app.factory.create_app()` under the `/admin` prefix.

## How This Route Should Behave

- Keep the dashboard read-only and template-focused.
- Keep persistence, validation, and endpoint CRUD in the NetBox route and service layers.
- Mask secrets if you expose more endpoint fields in the template.

## Extension Guidance

- Keep this module template-only for the dashboard and log-view-only for the API route.
- Add backend logic elsewhere and let this route stay a rendering layer.

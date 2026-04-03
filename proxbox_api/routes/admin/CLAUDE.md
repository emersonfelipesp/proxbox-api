# proxbox_api/routes/admin Module Guide

## Purpose

HTML admin dashboard for viewing the configured NetBox endpoint records.

## Current Files

- `__init__.py`: single FastAPI route that renders `templates/admin/index.html`.

## Current Behavior

- `GET /admin/` is excluded from OpenAPI and renders the dashboard with `GetNetBoxEndpoint` data.
- The route uses the shared `proxbox_api.templates` Jinja2 environment.
- The dashboard is mounted by `proxbox_api.app.factory.create_app()` under the `/admin` prefix.

## How This Route Should Behave

- Keep the route read-only and template-focused.
- Keep persistence, validation, and endpoint CRUD in the NetBox route and service layers.
- Mask secrets if you expose more endpoint fields in the template.

## Extension Guidance

- Keep this module template-only.
- Add backend logic elsewhere and let this route stay a rendering layer.

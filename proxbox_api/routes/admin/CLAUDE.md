# proxbox_api/routes/admin Module Guide

## Purpose

HTML admin dashboard for viewing the configured NetBox endpoint records.

## Current Files

- `__init__.py`: Single FastAPI route that renders `templates/admin/index.html`.

## Current Behavior

- `GET /admin/` is excluded from OpenAPI and renders the dashboard with `GetNetBoxEndpoint` data.
- The route uses the shared `proxbox_api.templates` Jinja2 environment.
- The dashboard is mounted by `proxbox_api.app.factory.create_app()` under the `/admin` prefix.

## Extension Guidance

- Keep this module template-only; put API and persistence logic in the route and service layers.
- Mask secrets in the template if you add more endpoint fields.

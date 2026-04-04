# docker/ Directory Guide

## Purpose

Container runtime configuration for the `proxbox-api` service. This directory holds nginx config templates, supervisord process configs, and shell entrypoints used by the multi-stage `Dockerfile` at the repo root.

## Files

| Path | Role |
|------|------|
| `nginx/` | nginx site configuration templates for HTTP and HTTPS (mkcert self-signed) reverse proxy |
| `supervisor/proxbox.conf` | supervisord program definition for the uvicorn process — hardcoded to `proxbox_api.main:app` |
| `entrypoint-runtime.sh` | Container entrypoint for the standard HTTP runtime image — starts nginx + supervisord |
| `entrypoint-mkcert.sh` | Container entrypoint for the HTTPS (mkcert) image variant |

## Dockerfile Overview

The `Dockerfile` at the repo root uses three stages:

1. **builder** — installs deps with `uv` into a virtualenv at `/app/.venv`
2. **runtime-base** — minimal Python image with the virtualenv copied in
3. **runtime** — adds nginx + supervisor, copies configs from `docker/`, exposes port 8000

The runtime image runs nginx (public-facing, port 8000) proxying to uvicorn (internal, port 8001).

## Key Notes

- `supervisor/proxbox.conf` runs `uvicorn proxbox_api.main:app` — update this if the ASGI entry point changes.
- The `APP_MODULE` build-arg was removed. The module is now hardcoded; build a custom image if a different module is needed.
- The mkcert variant generates a local TLS certificate at container startup via `entrypoint-mkcert.sh`.
- For Let's Encrypt / production TLS, configure nginx externally with cert volume mounts.

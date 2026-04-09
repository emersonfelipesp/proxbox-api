# docker/ Directory Guide

## Purpose

Container runtime configuration for the `proxbox-api` service. This directory holds nginx config templates, supervisord process configs, and shell entrypoints used by the multi-stage `Dockerfile` at the repo root.

## Files

| Path | Role |
|------|------|
| `nginx/proxbox-https.conf.template` | nginx HTTPS site config template (used by the nginx image) |
| `supervisor/proxbox.conf` | supervisord program definition — runs uvicorn on `127.0.0.1:8001` and nginx |
| `supervisor/supervisord.conf` | supervisord global config |
| `entrypoint-nginx.sh` | Entrypoint for the nginx image — generates mkcert certs, configures nginx, starts supervisord |
| `entrypoint-granian.sh` | Entrypoint for the granian image — generates mkcert certs, converts key to PKCS#8, starts granian |

## Dockerfile Overview

The `Dockerfile` at the repo root uses five stages:

1. **builder** — installs deps with `uv` into a virtualenv at `/app/.venv` (Alpine base, `python:3.13-alpine`)
2. **runtime-base** — minimal Alpine Python image with the virtualenv copied in
3. **raw** (default) — pure uvicorn, no proxy; `docker build .` produces this image
4. **nginx** — extends raw; adds nginx + supervisor + mkcert, HTTPS-only
5. **granian** — extends runtime-base; adds granian + mkcert, HTTPS-only via granian's native TLS

## Image Variants

| Stage | Tags | Protocol | Server |
|-------|------|----------|--------|
| `raw` | `latest`, `<version>` | HTTP | uvicorn on `0.0.0.0:PORT` |
| `nginx` | `latest-nginx`, `<version>-nginx` | HTTPS | nginx → uvicorn on `127.0.0.1:8001` |
| `granian` | `latest-granian`, `<version>-granian` | HTTPS | granian on `0.0.0.0:PORT` |

## Key Notes

- `supervisor/proxbox.conf` runs `uvicorn proxbox_api.main:app` — update this if the ASGI entry point changes.
- The nginx image always uses HTTPS; there is no HTTP-only nginx variant.
- The granian image requires the TLS key in PKCS#8 format; `entrypoint-granian.sh` converts it automatically with `openssl pkcs8`.
- For Let's Encrypt / production TLS, configure nginx externally with cert volume mounts.
- `TARGETARCH` build arg (set by BuildKit) is used instead of `dpkg --print-architecture` for Alpine compatibility when downloading the mkcert binary.

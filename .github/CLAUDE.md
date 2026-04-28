# .github/ Directory Guide

## Purpose

GitHub Actions CI/CD workflows for `proxbox-api`. All workflows live under `.github/workflows/`.

## Workflow Index

| File | Trigger | What it does |
|------|---------|--------------|
| `ci.yml` | Push / PR to any branch; Release published; manual dispatch | Lint (ruff), compile, import smoke checks, run `tests/` with coverage, then E2E Docker matrix (dev or pypi mode) |
| `docs.yml` | Push to `main` | Builds MkDocs site and deploys to GitHub Pages |
| `docker-hub-publish.yml` | Called by `publish-testpypi.yml` on Release, or manual dispatch | Builds and pushes three Alpine-based Docker images to Docker Hub: raw (uvicorn), nginx (nginx+mkcert+uvicorn), granian (granian+mkcert) |
| `publish-testpypi.yml` | GitHub Release published | Validates release metadata, builds dist, publishes to TestPyPI, validates install across Python 3.11–3.13, runs E2E pre-publish gate (dev deps), publishes to PyPI, then publishes Docker images and runs E2E post-publish verification (published artifacts) |
| `nightly-schema-refresh.yml` | Scheduled (nightly) | Runs `scripts/refresh_schemas.py` and opens a PR if schemas changed |
| `release-docker-verify.yml` | Release published | Post-release smoke test of all three published Docker images |

## CI Job Dependencies

```
ci.yml (push/PR — dev mode E2E only)
├── test
├── test-free-threaded (continue-on-error)
├── setup             (generates E2E matrix)
└── e2e-docker        (needs: test + setup; matrix of 6 transport combos × netbox_proxbox_mode)
    - dev mode:  netbox-proxbox from GitHub develop tarball
                 proxbox-api built from local checkout with DEV_OVERRIDES (netbox-sdk + proxmox-sdk from GitHub)
    - pypi mode: netbox-proxbox from PyPI; proxbox-api built from local checkout without overrides

ci.yml (release event — both dev + pypi modes)
└── e2e-docker matrix runs both netbox_proxbox_mode=dev and netbox_proxbox_mode=pypi

publish-testpypi.yml (GitHub Release published)
├── prepare-release        (validate tag/version, build dist, upload artifact)
├── publish-testpypi       (needs: prepare-release)
├── validate-testpypi      (needs: prepare-release + publish-testpypi; matrix py3.11/3.12/3.13)
├── e2e-pre-publish        (needs: prepare-release; dev deps — proxbox-api local build + DEV_OVERRIDES)
├── publish-pypi           (needs: prepare-release + validate-testpypi + e2e-pre-publish)
├── publish-docker         (needs: publish-pypi; calls docker-hub-publish.yml mode=publish)
└── e2e-post-publish       (needs: publish-docker + prepare-release; published Docker Hub image + PyPI netbox-proxbox)
```

## E2E Dependency Modes

| Mode | netbox-proxbox (in NetBox container) | proxbox-api container | netbox-sdk / proxmox-sdk (in proxbox-api) |
|------|--------------------------------------|-----------------------|-------------------------------------------|
| **dev** | GitHub `develop` branch tarball | Built from local checkout with `--build-arg DEV_OVERRIDES=...` | `git+https://github.com/emersonfelipesp/netbox-sdk.git@main` and `git+https://github.com/emersonfelipesp/proxmox-sdk.git@main` |
| **published** | PyPI `netbox-proxbox` | Docker Hub `emersonfelipesp/proxbox-api:<version>` | PyPI versions from `uv.lock` (no override) |

`DEV_OVERRIDES` is injected via `ARG DEV_OVERRIDES` in the Dockerfile builder stage. Normal production builds leave `DEV_OVERRIDES` empty (default `""`), so there is no impact on published images.

## Docker Image Tags

| Image | `latest` tag | Version tag |
|-------|-------------|-------------|
| Raw (uvicorn, HTTP) | `latest` | `<version>` |
| Nginx (nginx+mkcert, HTTPS) | `latest-nginx` | `<version>-nginx` |
| Granian (granian+mkcert, HTTPS) | `latest-granian` | `<version>-granian` |

All tags also have `sha-<commit>` variants (e.g., `sha-abc1234`, `sha-abc1234-nginx`, `sha-abc1234-granian`).

## Key Rules

- The `uv.lock` at the repo root must stay in sync with `pyproject.toml` because CI runs `uv sync --frozen`.
- Release workflows validate that the `pyproject.toml` version matches the Git tag before publishing.
- Do not add secrets to workflow files — use repository secrets (`PYPI_TOKEN`, `DOCKERHUB_TOKEN`, etc.).

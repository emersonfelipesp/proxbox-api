# .github/ Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/.github/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

GitHub Actions CI/CD workflows for `proxbox-api`. All workflows live under `.github/workflows/`.

## Workflow Index

| File | Trigger | What it does |
|------|---------|--------------|
| `ci.yml` | Push / PR to any branch; Release published; manual dispatch | Lint (ruff), compile, import smoke checks, run `tests/` with coverage, then E2E Docker matrix (dev or pypi mode). Docker-backed E2E runs with the `mock_http` marker; the in-process MockBackend pass runs separately. |
| `docs.yml` | Push to `main` | Builds MkDocs site and deploys to GitHub Pages |
| `docker-hub-publish.yml` | Called by `publish-testpypi.yml` on Release, or manual dispatch | Builds and pushes three Alpine-based Docker images to Docker Hub: raw (uvicorn), nginx (nginx+mkcert+uvicorn), granian (granian+mkcert) |
| `publish-testpypi.yml` | Version tag push, GitHub Release published, or manual dispatch | Validates release metadata, builds dist, then runs either the TestPyPI lane or the PyPI lane. Normal/post tag pushes publish to TestPyPI; rc tag pushes, releases, and `publish_target=pypi` dispatches publish to PyPI. PyPI success then publishes Docker images and runs post-publish E2E. |
| `nightly-schema-refresh.yml` | Scheduled (nightly) | Runs `scripts/refresh_schemas.py` and opens a PR if schemas changed |
| `release-docker-verify.yml` | Release published | Post-release smoke test of all three published Docker images |

## CI Job Dependencies

```
ci.yml (push/PR — dev mode E2E only)
├── test
├── test-py311-floor
├── test-free-threaded (continue-on-error)
├── docker-bind-smoke  (raw + granian bind-host startup checks)
├── setup             (generates E2E matrix)
├── build-netbox-image (only uploads an artifact when the public NetBox image cannot be pulled)
└── e2e-docker        (needs: test + setup + build-netbox-image; transport × NetBox version matrix)
    - dev mode:  netbox-proxbox from GitHub develop tarball
                 proxbox-api built from local checkout with DEV_OVERRIDES (netbox-sdk + proxmox-sdk from GitHub)
    - pypi mode: netbox-proxbox from PyPI; proxbox-api built from local checkout without overrides
    - NetBox image handling: each E2E job pulls the public image first and only downloads the source-built artifact when the registry pull fails.
    - NetBox readiness waits up to 20 minutes for migrations/search indexing, then checks `/api/status/` before creating tokens.
    - Docker-backed Proxmox E2E uses pytest marker `mock_http`; the separate in-process MockBackend pass uses `mock_backend`.

ci.yml (release event — both dev + pypi modes)
└── e2e-docker matrix runs both netbox_proxbox_mode=dev and netbox_proxbox_mode=pypi

publish-testpypi.yml (staged package release)
├── prepare-release        (validate tag/version, build dist, upload artifact)
├── TestPyPI lane
│   ├── publish-testpypi   (needs: prepare-release)
│   └── validate-testpypi  (needs: prepare-release + publish-testpypi; installs package from TestPyPI across py3.11/3.12/3.13, then runs local checks)
└── PyPI lane
    ├── validate-pypi-candidate (needs: prepare-release; local checks across py3.11/3.12/3.13)
    ├── e2e-pre-publish         (needs: prepare-release; dev deps — proxbox-api local build + DEV_OVERRIDES; same 20-minute NetBox readiness gate)
    ├── publish-pypi            (needs: prepare-release + validate-pypi-candidate + e2e-pre-publish)
    ├── validate-pypi           (needs: prepare-release + publish-pypi; installs package from PyPI)
    ├── publish-docker          (needs: prepare-release + validate-pypi; calls docker-hub-publish.yml mode=publish)
    └── e2e-post-publish        (needs: publish-docker + prepare-release; published Docker Hub image + PyPI netbox-proxbox; same 20-minute NetBox readiness gate)
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
- Package uploads intentionally do not use `twine --skip-existing`; if an artifact version was consumed, bump to the next `.postN` or `rcN` and publish that immutable version.
- Do not add secrets to workflow files — use repository secrets (`PYPI_TOKEN`, `DOCKERHUB_TOKEN`, etc.).
- Keep `docs/development/ci-e2e-workflows.md`, `docs/pt-BR/development/ci-e2e-workflows.md`, and `docs/development/release-publishing.md` aligned with CI workflow changes.

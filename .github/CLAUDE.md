# .github/ Directory Guide

## Purpose

GitHub Actions CI/CD workflows for `proxbox-api`. All workflows live under `.github/workflows/`.

## Workflow Index

| File | Trigger | What it does |
|------|---------|--------------|
| `ci.yml` | Push / PR to any branch | Lint (ruff), compile, import smoke checks, run `tests/` with coverage |
| `docs.yml` | Push to `main` | Builds MkDocs site and deploys to GitHub Pages |
| `docker-hub-publish.yml` | Called by `ci.yml` on `main` push | Builds and pushes the `proxbox-api` runtime Docker image to Docker Hub |
| `publish-testpypi.yml` | GitHub Release published | Validates release metadata, publishes `proxbox_api` to TestPyPI, validates install across Python 3.11–3.13, publishes to PyPI |
| `nightly-schema-refresh.yml` | Scheduled (nightly) | Runs `scripts/refresh_schemas.py` and opens a PR if schemas changed |
| `release-docker-verify.yml` | Release published | Post-release smoke test of published Docker image |

## CI Job Dependencies

```
ci.yml
├── test          (lint + tests for proxbox_api)
└── docker-images (only on main push, needs: test)
```

## Key Rules

- The `uv.lock` at the repo root must stay in sync with `pyproject.toml` because CI runs `uv sync --frozen`.
- Release workflows validate that the `pyproject.toml` version matches the Git tag before publishing.
- Do not add secrets to workflow files — use repository secrets (`PYPI_TOKEN`, `DOCKERHUB_TOKEN`, etc.).

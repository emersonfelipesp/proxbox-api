# docs/ Directory Guide

## Purpose

MkDocs Material documentation for `proxbox-api`, published in English and Brazilian Portuguese. The `mkdocs.yml` at the repo root configures the site with `mkdocs-static-i18n` for bilingual support.

## Directory Structure

```
docs/
├── index.md                    # Main landing page (English)
├── getting-started/            # Installation and configuration guides
├── development/                # Contributing, deployment, troubleshooting, testing
├── architecture/               # System overview and design patterns
├── api/                        # HTTP and WebSocket API reference
├── sync/                       # Sync workflow documentation
└── pt-BR/                      # Brazilian Portuguese translations
    ├── api/
    ├── architecture/
    ├── development/
    ├── getting-started/
    └── sync/
```

## Building and Serving Docs

```bash
# Install docs dependencies
uv sync --extra docs

# Serve locally with live reload
uv run mkdocs serve

# Build static site
uv run mkdocs build
```

## Content Guidelines

- Keep English (`docs/`) and Portuguese (`docs/pt-BR/`) files in sync when updating content.
- API reference in `docs/api/` should match the actual route signatures in `proxbox_api/routes/`.
- Architecture diagrams belong in `docs/architecture/`.
- Do not store generated artifacts or runtime data in `docs/`.

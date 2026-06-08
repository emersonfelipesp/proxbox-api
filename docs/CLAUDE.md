# docs/ Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/docs/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

MkDocs Material documentation for `proxbox-api`, published in English and Brazilian Portuguese. The `mkdocs.yml` at the repo root configures the site with `mkdocs-static-i18n` for bilingual support.

## Directory Structure

```
docs/
├── index.md                    # Main landing page (English)
├── getting-started/            # Installation and configuration guides
│   ├── installation.md
│   ├── configuration.md
│   └── authentication.md
├── development/                # Contributing, deployment, troubleshooting, testing
├── architecture/               # System overview and design patterns
├── api/                        # HTTP and WebSocket API reference
│   ├── http-reference.md
│   ├── websocket-reference.md
│   ├── cache.md
│   └── cluster-ha.md
├── sync/                       # Sync workflow documentation
│   ├── workflows.md
│   ├── reconciliation-architecture.md
│   ├── name-collision-resolver.md
│   ├── overwrite-flags.md
│   └── scheduler-container.md
├── operations/                 # Operational guides
│   ├── firecracker.md          # Firecracker host-agent provisioning
│   └── hardware-discovery.md   # Hardware discovery and DCIM sync
└── pt-BR/                      # Brazilian Portuguese translations
    ├── api/
    ├── architecture/
    ├── development/
    ├── getting-started/
    ├── operations/
    └── sync/
        └── reconciliation-architecture.md
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
- Reconciliation engine docs live under `docs/sync/reconciliation-architecture.md`
  and `docs/pt-BR/sync/reconciliation-architecture.md`; keep them aligned
  with `proxbox_api/services/sync/reconciliation/` and `proxbox-reconcile-rs/`.
- Do not store generated artifacts or runtime data in `docs/`.

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
├── development/                # Contributing, deployment, troubleshooting, testing, async internals
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
│   ├── custom-fields.md        # Custom-field reconcile and recovery procedure
│   ├── ceph-write-approvals.md # Ceph v2 approval, recovery, rollout, NPR evidence
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
- Ceph v2 write behavior, failure recovery, deployment/rollback, and bounded
  NPR 7150.2D feature evidence live in `operations/ceph-write-approvals.md`;
  keep its Portuguese translation aligned, never document legacy inline apply
  as authorized, preserve exact node/typed-payload/lease-owner/unique-UPID and
  non-Proxmox capability constraints, and never promote feature evidence to a
  project compliance or certification claim.
- API reference in `docs/api/` should match the actual route signatures in `proxbox_api/routes/`.
- VM interface sync docs must describe `vm_interface_sync_strategy` with
  `guest_os_model` as the default and `legacy_rename` as deprecated
  compatibility mode. Public VM interface stream routes are owned by
  `read_vm.py`; do not document `interfaces_vm.py` as a separate route source.
- Architecture diagrams belong in `docs/architecture/`.
- Reconciliation engine docs live under `docs/sync/reconciliation-architecture.md`
  and `docs/pt-BR/sync/reconciliation-architecture.md`; keep them aligned
  with `proxbox_api/services/sync/reconciliation/` and `proxbox-reconcile-rs/`.
- Do not store generated artifacts or runtime data in `docs/`.

## Async / Performance Developer Guide

Six pages under `docs/development/` document how proxbox-api manages async I/O,
concurrency, and event-loop safety in the VM sync pipeline:

| File | Topic |
|---|---|
| `async-overview.md` | Single-threaded event loop model, `asyncio.to_thread`, building blocks |
| `async-semaphores.md` | `asyncio.Semaphore` patterns, three semaphores in the sync pipeline, failure isolation |
| `async-gather.md` | `asyncio.gather` with and without `return_exceptions`, three gather patterns |
| `async-two-phase-batch.md` | Two-phase VM batch design, `_PreparedVMState` hand-off, failure counting |
| `async-timeout-scoping.md` | `_scoped_proxmox_backend_timeout`, widen-only invariant, depth counter, guest-agent timeout |
| `async-tunables.md` | All async env vars + plugin settings keys, diagnostics, tuning examples |

Each page has a corresponding `docs/pt-BR/development/` translation. Both sets
must stay in sync when the underlying async behavior changes.

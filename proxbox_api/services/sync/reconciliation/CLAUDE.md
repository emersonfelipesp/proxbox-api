# proxbox_api/services/sync/reconciliation Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/services/sync/reconciliation/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Pure, synchronous reconciliation seams used by sync routes. The current seam is
the VM operation-queue builder extracted from
`proxbox_api/routes/virtualization/virtual_machines/sync_vm.py`.

The service turns prepared VM state and a NetBox VM snapshot into deterministic
NetBox operations:

```text
CREATE | GET | UPDATE + patch_payload
```

No HTTP, async, database access, auth, retry handling, dispatch execution, SSE,
or WebSocket code belongs here.

## Current Files

- `__init__.py`: public reconciliation package exports.
- `types.py`: `PreparedVMState`, `NetBoxVMOperation`, and shared key aliases.
- `vm_queue.py`: engine-neutral VM queue entry point, Python implementation,
  Rust compare/rust dispatch, mismatch diffing, and operation adaptation.
- `rust_bridge.py`: Pydantic v2 JSON-byte bridge into the optional
  `proxbox-reconcile-rs` native package.
- `metrics.py`: mismatch counter plumbing exposed through cache metrics.

## Engine Modes

- `PROXBOX_RECONCILIATION_ENGINE=python`: default. Always available.
- `PROXBOX_RECONCILIATION_ENGINE=compare`: run Python and Rust, log/increment
  mismatches, return Python output.
- `PROXBOX_RECONCILIATION_ENGINE=rust`: return Rust output. Requires
  `proxbox-reconcile-rs` to be installed.
- `PROXBOX_RECONCILIATION_COMPARE_STRICT=true`: raise on mismatch in compare
  mode. Use in CI and local parity debugging.

## Parity Rules

- Preserve input order in output operations.
- Include `vm_type` in VM identity/adaptation keys to avoid QEMU/LXC collisions
  when both have the same VMID in a cluster.
- Treat `2048` and `2048.0` as equal.
- Compare tags order-independently, but preserve merge semantics when
  `overwrite_vm_tags=True`.
- Keep relation handling tolerant of both integer IDs and nested objects with
  `id`.
- Preserve documented `custom_fields: {"foo": None}` versus `{}` behavior.
- If NetBox lacks the `virtual_machine_type` field, do not generate a patch for
  that field.

## Checks

Run these for this directory:

```bash
uv run pytest tests/reconciliation -q
```

When the Rust package is installed or changed, run strict parity:

```bash
cargo test --no-default-features --manifest-path proxbox-reconcile-rs/Cargo.toml
uv pip install -e proxbox-reconcile-rs
PROXBOX_RECONCILIATION_ENGINE=compare \
  PROXBOX_RECONCILIATION_COMPARE_STRICT=true \
  uv run pytest tests/reconciliation -q
```

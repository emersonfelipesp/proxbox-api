# proxbox-reconcile-rs Agent Guide

Read `CLAUDE.md` first for the full architecture and rollout rules.

## Quick Rules

- This crate is optional; Python remains the default reconciliation engine.
- Keep the crate pure: no HTTP, async runtime, SQLite, auth, retries, dispatch,
  or streaming.
- Preserve parity with `build_vm_operation_queue_python()`.
- Keep QEMU/LXC identity keys typed with `vm_type`.
- Run Rust tests and strict Python/Rust parity tests before pushing changes.

## Required Checks

```bash
cargo test --no-default-features --manifest-path proxbox-reconcile-rs/Cargo.toml
uv pip install -e proxbox-reconcile-rs
PROXBOX_RECONCILIATION_ENGINE=compare \
  PROXBOX_RECONCILIATION_COMPARE_STRICT=true \
  uv run pytest tests/reconciliation -q
```

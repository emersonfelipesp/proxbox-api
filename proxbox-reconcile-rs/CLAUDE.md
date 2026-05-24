# proxbox-reconcile-rs Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox-reconcile-rs/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Optional PyO3/maturin Rust package for the deterministic VM operation-queue
reconciliation seam in `proxbox-api`.

This package is intentionally separate from the FastAPI backend. It has no
HTTP clients, async runtime, SQLite access, auth, retry logic, dispatch
execution, or streaming. It receives JSON bytes, computes the queue, and
returns JSON bytes.

## Boundary

Input:

```text
prepared_vms + netbox_snapshot + flags
```

Output:

```text
CREATE | GET | UPDATE operations with patch_payload
```

The Python bridge lives in
`proxbox_api/services/sync/reconciliation/rust_bridge.py`. The engine-neutral
wrapper and Python fallback live in
`proxbox_api/services/sync/reconciliation/vm_queue.py`.

## Runtime Rules

- Python remains the default engine: `PROXBOX_RECONCILIATION_ENGINE=python`.
- `compare` mode runs Python and Rust, returns Python, and records mismatches.
- `rust` mode requires this package to be installed and should only be enabled
  after compare mode is clean in the target environment.
- The PyO3 entry point must copy Python-owned bytes into `Vec<u8>` before
  releasing the GIL.
- Keep the Rust implementation deterministic. Preserve VM order and use stable
  map behavior where output order matters.
- Include `vm_type` in all VM identity keys to avoid QEMU/LXC VMID collisions.
- Match Python's loose number semantics for integer/float JSON comparisons.
- Keep tag comparison order-independent and preserve the documented
  custom-field null/missing semantics.

## Files

- `Cargo.toml`: crate metadata, PyO3, serde, serde_json, thiserror, indexmap.
- `pyproject.toml`: maturin build backend and native module path.
- `src/lib.rs`: PyO3 module and GIL-release wrapper.
- `src/vm.rs`: queue input/output structs and VM operation builder.
- `src/normalize.rs`: relation, tag, custom-field, and current-record normalization.
- `src/diff.rs`: loose JSON diffing and overwrite-rule application.
- `python/proxbox_reconcile_rs/__init__.py`: Python package re-export surface.
- `tests/`: import smoke coverage for the built extension.

## Checks

Run these when changing this package or its Python bridge:

```bash
cargo test --no-default-features --manifest-path proxbox-reconcile-rs/Cargo.toml
uv pip install -e proxbox-reconcile-rs
PROXBOX_RECONCILIATION_ENGINE=compare \
  PROXBOX_RECONCILIATION_COMPARE_STRICT=true \
  uv run pytest tests/reconciliation -q
```

For a wheel smoke test:

```bash
uv run --with maturin maturin build --release \
  --out /tmp/proxbox-reconcile-dist \
  --manifest-path proxbox-reconcile-rs/Cargo.toml
```

## Rollout Guidance

Do not make Rust the default based on microbenchmarks alone. The initial live
measurement showed VM reconciliation was a tiny share of real sync wall time,
and full synthetic Rust-path benchmarks were slower after serialization and
adaptation overhead. Keep Python default unless future real-sync evidence shows
the full Rust path is faster and parity has stayed clean.

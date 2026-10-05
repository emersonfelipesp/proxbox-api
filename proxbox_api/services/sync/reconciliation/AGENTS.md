# proxbox_api/services/sync/reconciliation Directory Guide

## Workspace Context

This file lives at `<repository-root>/proxbox_api/services/sync/reconciliation/CLAUDE.md` inside the `personal-context` workspace.
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

The runtime value is read from `ProxboxPluginSettings.reconciliation_engine`
through `proxbox_api.runtime_settings.get_plugin_str()`. This engine selector is
plugin-settings only; backend environment variables must not override it.

- `reconciliation_engine=python`: default. Always available.
- `reconciliation_engine=compare`: run Python and Rust, log/increment
  mismatches, return Python output.
- `reconciliation_engine=rust`: return Rust output. Requires
  `proxbox-reconcile-rs` to be installed.
- `reconciliation_compare_strict=true`: raise on mismatch in compare
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
- Validate the creation-only `platform` relation before engine selection. The
  Python, compare, and Rust modes must all reject non-positive scalar or nested
  IDs even though platform is omitted from existing-record diff payloads.
- If NetBox lacks the `virtual_machine_type` field, do not generate a patch for
  that field.
- `PreparedVMState.desired_state` is the optional canonical Pydantic model
  finalized after name resolution. The Python planner must reuse it when
  present and preserve validation fallback for other callers. Benchmark fixture
  adapters must carry `sync_state_fields`; dropping them silently benchmarks
  cluster fallback instead of endpoint-first identity.

## Cluster guard (Python and Rust)

Rows with no cluster data (verdict `unknown`) and explicit `cluster: null` rows (verdict `unassigned`) are never selected while the live cluster is known; `skip_unverifiable_vm_candidates` skips such a prepared VM rather than creating a duplicate or letting a colliding cluster adopt it. It runs in `build_vm_operation_queue` before either engine (so Rust cannot emit a CREATE Python skips) and again first in `build_vm_operation_queue_python`; `unverifiable_vm_warnings` reports each skip as a stage warning. A Rust pick that fails the guard and yields no Python operation is dropped.

`select_existing_vm_record` drops an endpoint-keyed match whose NetBox VM lives
in a different cluster than the prepared VM (endpoint ids are not unique across
clusters) and falls back to the endpoint candidate that belongs to the live
cluster, so a shadowed first-wins index entry cannot hide the right VM or cause a
write to another cluster's VM. The Rust engine (`proxbox-reconcile-rs/src/vm.rs`)
was **not** changed and still matches on the endpoint key alone. To keep the
`rust` and `compare` engines from queuing writes against another cluster's VM,
`_build_vm_operation_queue_with_rust` post-filters its operations
(`_reject_cross_cluster_operations`): a cross-cluster GET/UPDATE is rebuilt with
`build_vm_operation_queue_python` for that one prepared VM over the full NetBox
snapshot, so the live cluster's own record is selected (same selector and patch
computation as the Python engine) and a CREATE is queued only when the live
cluster has no candidate. Both snapshot orders therefore agree with the Python
engine, including in `compare` mode. Tests fake the Rust output, so the native
package is not required.

## Checks

Run these for this directory:

```bash
uv run pytest tests/reconciliation -q
```

When the Rust package is installed or changed, run strict parity:

```bash
cargo test --no-default-features --manifest-path proxbox-reconcile-rs/Cargo.toml
uv pip install -e proxbox-reconcile-rs
uv run pytest tests/reconciliation -q
```

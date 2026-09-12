# Mounted Operation Inventory Guide

## Purpose and Boundaries

This package provides developer-only, offline evidence for the actual mounted
FastAPI operation surface. It is not an authorization catalog or a runtime
startup hook. Read the root guides and `docs/operations/operation-inventory.md`
before changing it. The explicit CLI installs isolation before importing the
application, mounts the real factory and explicitly registers bundled generated
routes without lifespan, bootstrap, database access, handler calls or sockets.

## Contracts and Extension Rules

- `schema.py` owns strict versioned wire records and bounded canonical bytes.
  Reject unknown and duplicate keys, invalid Unicode, coercion and stale joins.
- `adapter.py` is pinned to reviewed FastAPI and Starlette versions and exact
  routing source bytes. Preserve every occurrence and effective path, including
  nested routers, prefixed WebSockets, actual ASGI mounts and collisions.
- `inputs.py`, `generated.py` and `collection.py` require all twenty-two reviewed
  feature inputs, fifteen reachable states, all optional router imports, and
  exact generated version/alias sequences. The all-disabled state is
  unreachable; do not add a fictional configuration to satisfy a count.
- `provenance.py` binds the full extraction source closure and unchanged locked
  environment. Only the exact same-source editable install plus source metadata
  representation is supported; conflicting versions or origins fail closed.
- `coverage.py` preserves explicit unresolved evidence for every required
  parent column. Effects are never inferred from HTTP methods. Generation and
  drift verification do not imply readiness or authorize a managed operation.
- `rendering.py` emits escaped English and Portuguese tables under `contracts/`.
  Handwritten documentation embeds these with restricted, fail-on-missing
  snippets. Generated documents do not belong under `docs/`.
- `verification.py` rejects stale source, dependency and callable identities
  before evaluating coverage readiness.

Run the focused `tests/operation_inventory/` suite, explicit regeneration and
drift verification, strict bilingual documentation build, configured lint,
per-function complexity, branch evidence and full native gates after changes.
Keep the repository's McCabe threshold of ten, including nested functions.
Do not update dependencies, workflows, runtime routes or activation settings as
an incidental part of inventory maintenance.

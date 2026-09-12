# Mounted operation inventory

The maintained inventory records registrations, not permission to execute them.
It constructs the real application and explicitly registers generated routes in
an isolated offline child. It never enters lifespan, initializes a database,
invokes a route or dependency, or contacts a managed system. The child does not
inherit credentials, explicitly disables dotenv loading, and rejects socket
operations and writes outside its temporary output directory. This Python audit guard is a developer safety
boundary, not a production syscall sandbox.

## Generate and verify

Use the repository's unchanged lock and supported Python environment:

```bash
uv sync --locked --extra test --extra docs --extra pbs --extra pdm
uv run python scripts/mounted_operation_inventory.py generate
uv run python scripts/mounted_operation_inventory.py verify
uv run python scripts/mounted_operation_inventory.py readiness
uv run mkdocs build --strict
```

Generation publishes deterministic JSON, schemas, and bilingual tables under
`contracts/`. It creates an explicitly unresolved coverage document only when
one does not exist; it never overwrites an operator's coverage dispositions.
After regeneration, reconcile the coverage document with the new inventory
digest. Verification recollects the real registration surface and fails on
drift or an input it cannot evaluate. Missing optional packages and failed
generated schemas are errors, not smaller successful inventories.

Readiness is a separate record-completeness check. Exit status `2` means required
operation columns are unresolved. A mechanically complete coverage record is
not authorization, independent review, or production activation approval.
Generation and documentation can succeed while readiness correctly fails.

## Scope and limits

The fixed matrix contains twenty-two inputs and fifteen reachable inclusion
states: seven nonempty sidecar-only subsets and eight core-present subsets.
Default-all and explicit core-all are equivalent. The sixteenth Boolean state,
with neither core nor sidecars, is unreachable: empty input selects all features
and unknown-only input selects core. Whitespace, case, and repeated tokens are
explicit regression cases. Generated routes appear in every selected mode,
because real lifespan registers them independently of the core selection.

The version-aware adapter is pinned to the reviewed FastAPI and Starlette pair.
Framework upgrades require an adapter review and the nested HTTP, WebSocket,
mount, and collision oracles. Every registration occurrence and its order is
retained even when operation definitions are identical. Generated aliases retain
their original operation, upstream path/method, schema version, and digest.
Source paths are repository- or distribution-relative; timestamps, absolute
workspace paths, and whole-commit self-references are excluded.

Exact path/method overlaps are observable, but this does not establish complete
semantic precedence for parameterized routes, catch-all paths, or opaque mounts.
HTTP methods never determine effect classification. Local persistence, NetBox
mutation, material reveal, managed reads and writes, and interactive capability
creation/consumption are separate coverage categories. Eager dependencies can
have effects before handler authentication, and an existing terminal ticket can
outlive a creator-only policy change. Both sides require independent tracing.

The full caller/effect matrix remains incomplete. Each operation requires caller
and final-handler evidence, target and procedure/version, transport, input and
result schemas, credential purpose/fields, endpoint/transport gates, permissions
and approval, timeout, idempotency/reconciliation, terminal task proof, audit
links, and tests. Unknown columns remain unresolved. No runtime flag, write
gate, authentication behavior, SSH policy, or deployment behavior changes here.

## Registration table

The mode summary covers every input. The detailed table retains every default
registration; the machine artifact retains the complete ordered sequence for
each mode. The build checks source and table integrity before embedding these
local generated snippets. Missing, stale, or symlinked inputs fail the build.

--8<-- "mounted-operations.en.md"

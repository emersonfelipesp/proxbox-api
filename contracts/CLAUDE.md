# Mounted Operation Contract Artifacts

`operation-inventory-inputs.json` is the reviewed handwritten extraction input
manifest. The explicit developer CLI generates `mounted-operations.json`, its
strict schema, bilingual Markdown tables, the coverage schema and the integrity
manifest. It creates an unresolved `operation-coverage.json` only when absent;
existing coverage is never overwritten. Coverage must bind the exact inventory
digest and explicitly address every required parent column before readiness can
pass. Changing source evidence invalidates stale coverage rather than silently
transferring an old authorization disposition.

Read `proxbox_api/operation_inventory/CLAUDE.md` and the handwritten operations
guide before changing these files. Do not manually edit generated registrations,
omit repeated occurrences or WebSockets, collapse generated aliases, infer
effects from HTTP methods, or classify untraced callers as safe. Preserve every
one of the twenty-two input oracles and the fifteen reachable-state proof.

Generated artifacts remain in this directory, never under `docs/`. The MkDocs
build-only hook checks exact regular-file source and artifact digests before
restricted snippets embed the English and Portuguese tables. Missing or stale
evidence is a build failure. Generation, drift verification, coverage readiness,
runtime authorization and release activation are distinct gates.

# proxbox-api 0.0.22.post1

## Summary

This critical security hotfix removes generated Python source evaluation from
runtime Proxmox model loading, disables runtime code generation by default,
makes bundled schemas immutable, verifies the provenance of development-only
user schemas and route caches, and bounds generated OpenAPI documents before
persistence or model construction.

## Security changes

- Runtime route registration now constructs Pydantic request and response
  models directly from parsed OpenAPI data with `pydantic.create_model`.
- `PROXBOX_RUNTIME_CODEGEN_ENABLED` is a process-level boolean that defaults to
  `false`. The default application route table excludes
  `POST /proxmox/viewer/generate` and
  `POST /proxmox/viewer/routes/refresh`; schema discovery, route registration,
  and source rendering use bundled schemas only. The setting is a
  development-only opt-in and must remain disabled in production.
- The offline Python renderer validates emitted identifiers and represents JSON
  aliases, descriptions, and defaults as Python literals.
- Code-generation version tags use a bounded character grammar and reject
  parent-directory names.
- Every generated artifact path and the runtime route-cache path is resolved
  and verified to remain inside its configured base directory before access.
- Version tags are rejected before a Playwright crawl or filesystem write can
  begin.
- Bundled version tags always resolve before user-generated artifacts and
  cannot be overwritten through persisted generation.
- Official-source user artifacts require `provenance.json` with the source URL,
  generation time, and matching SHA-256 digest of `openapi.json`.
- Provenance sidecars provide corruption detection, not authentication. A
  process running under the same operating-system user can forge an artifact
  and its matching digest. This is why production does not admit user schemas.
- Non-default-source artifacts are isolated under `custom/<version_tag>/` for
  offline inspection and are never considered for runtime route registration.
- `GET /proxmox/viewer/pydantic` always renders from the validated OpenAPI
  document and never returns persisted Python source. Rendering is cached by
  verified schema digest, runs off the event loop, has a 2 MiB output ceiling,
  and is limited to six requests per minute per source.
- Fixed limits cover document bytes, schema depth, paths, operations,
  properties, generated models, and metadata strings. Unsafe normalized field
  names, collisions, and duplicate generated model names are rejected before
  any model is constructed.
- Runtime route caches require their own matching provenance digest. An invalid
  or over-limit cache is ignored without replacing an in-process
  last-known-good route set.
- Cache and provenance writes use contained, no-follow atomic file creation,
  and route refresh persists both before swapping the mounted route set.
- Startup and the CLI quarantine invalid caches and orphan sidecars under an
  interprocess lock with no-follow, no-clobber moves.
- Aggregate registration is capped at 8 eligible versions, 32 MiB of OpenAPI
  documents, 16,384 models, and 32,768 routes before model or cache
  construction.
- The mounted-operation inventory now includes bundled generated proxy routes
  in every default feature mode, matching lifespan startup. The default-all
  inventory contains 4,047 registrations, while the development opt-in contains
  4,049.
- `proxbox-schema list` and `proxbox-schema status` now discover bundled schemas
  only by default. The explicit `--include-user` flag requires
  `PROXBOX_RUNTIME_CODEGEN_ENABLED=true`, and output labels user-generated
  artifacts separately.

## Compatibility

This release requires no database migration. Bundled generated route paths,
model names, JSON aliases, and read-only proxy semantics remain compatible with
version `0.0.22`. Runtime HTTP generation, route refresh, and user-schema
discovery now require the explicit development-only opt-in. A bundled tag,
including `latest`, cannot be refreshed in place; package replacement is the
sole supported update path.

## Upgrade

Deploy the exact `proxbox-api 0.0.22.post1` package through the approved release
workflow.

Before starting any `0.0.22.post1` worker, stop the previous workers and run:

```bash
proxbox-schema quarantine-legacy
```

This mandatory, idempotent cleanup renames every user-generated
`pydantic_models.py` and every runtime route cache without valid provenance to a
`.quarantined-<UTC timestamp>` name. Application lifespan startup repeats the
same quarantine before route registration, but the explicit command provides a
reviewable upgrade record and completes cleanup before the service accepts
traffic. Preserve quarantined files until the deployment is validated, then
remove them through the operator's normal retention process. Restart all
backend workers so each process rebuilds its in-memory models from immutable
bundled schemas. Development instances that intentionally need user-schema
discovery must set `PROXBOX_RUNTIME_CODEGEN_ENABLED=true` before startup.

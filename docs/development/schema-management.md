# Proxmox Schema Management

`proxbox-api` ships bundled Proxmox OpenAPI schemas for the latest stable PVE
release lines. These schemas drive the runtime-generated proxy routes under
`/proxmox/api2/*`. Runtime code generation is disabled by default. Set
`PROXBOX_RUNTIME_CODEGEN_ENABLED=true` only in a development environment when
HTTP generation, route refresh, or user-generated schema discovery is needed.

## Bundled schemas

The following versions are included with the package under `proxbox_api/generated/proxmox/`:

| Version tag | Proxmox release |
|-------------|-----------------|
| `8.1`       | PVE 8.1.x       |
| `8.2`       | PVE 8.2.x       |
| `8.3`       | PVE 8.3.x       |
| `latest`    | Current API Viewer snapshot |

Bundled tags are immutable. With the default setting, startup and schema
rendering use bundled schemas only and never scan the user-generated directory
for schemas. Refreshing a bundled tag, including `latest`, is supported only by
installing a replacement package that contains the updated schema. The startup
log confirms which bundled versions were found:

```
[INFO] Bundled Proxmox OpenAPI schema versions available: 8.1, 8.2, 8.3, latest
```

## Automatic version detection

When you call `GET /proxmox/sessions`, the app checks the connected Proxmox cluster's version against the bundled schemas. Each session entry in the response includes a `schema_status` field:

```json
{
  "name": "pve-cluster",
  "proxmox_version": {"version": "8.3.2", "release": "8.3", "repoid": "abc123"},
  "schema_release": "8.3",
  "schema_status": {"status": "available", "version_tag": "8.3"}
}
```

If the connected version has no matching bundled schema, the default response
reports that runtime generation is disabled. When the development-only opt-in
is enabled, generation can start automatically in the background:

```json
{
  "schema_status": {
    "status": "generating",
    "version_tag": "8.4",
    "message": "No bundled schema found for Proxmox 8.4. Background generation started. This may take several minutes."
  }
}
```

Routes for the new version are registered once generation completes only when
`PROXBOX_RUNTIME_CODEGEN_ENABLED=true`.

## CLI: `proxbox-schema`

The `proxbox-schema` command is the recommended way to manage schemas manually.

### List available versions

```bash
proxbox-schema list
```

Output:

```text
Available Proxmox OpenAPI schema versions (4):
         8.1   6.4 MB   [bundled]   /opt/proxbox_api/generated/proxmox/8.1/openapi.json
         8.2   6.4 MB   [bundled]   /opt/proxbox_api/generated/proxmox/8.2/openapi.json
         8.3   6.4 MB   [bundled]   /opt/proxbox_api/generated/proxmox/8.3/openapi.json
      latest   7.3 MB   [bundled]   /opt/proxbox_api/generated/proxmox/latest/openapi.json
```

`list` and `status` inspect bundled schemas only by default and do not access
the user-generated directory. In an opted-in development process, add
`--include-user` to either command to include provenance-verified user
artifacts. The output labels these artifacts as `user-generated`. The flag is
rejected unless `PROXBOX_RUNTIME_CODEGEN_ENABLED=true`.

### Check status

```bash
proxbox-schema status
```

Shows available versions and any active or recently completed generation tasks:

```
Bundled versions: 8.1, 8.2, 8.3, latest
No active or recent generation tasks.
```

### Generate a schema

```bash
proxbox-schema generate 8.4
```

This crawls the official Proxmox API Viewer, parses all endpoints, and writes
the generated artifacts under the user-generated schema directory. The default
is `$XDG_DATA_HOME/proxbox/generated/proxmox`, or
`~/.local/share/proxbox/generated/proxmox` when `XDG_DATA_HOME` is unset. The
command prints progress and a completion summary:

```
Generating Proxmox OpenAPI schema for version '8.4'...
Output directory: /var/lib/proxbox/generated/proxmox/8.4
Source URL: https://pve.proxmox.com/pve-docs/api-viewer/
Workers: 10

This may take several minutes. The pipeline crawls the Proxmox API Viewer,
parses all endpoints, and generates OpenAPI + Pydantic artifacts.

Generation completed for Proxmox 8.4
  Endpoints:  493
  Operations: 1284
  Duration:   187.3s
  Output:     /var/lib/proxbox/generated/proxmox/8.4

Schema is ready for offline inspection.
Start the development app with PROXBOX_RUNTIME_CODEGEN_ENABLED=true to discover it.
```

In an opted-in development instance, register the new routes without restarting:

```bash
curl -s -X POST http://localhost:8800/proxmox/viewer/routes/refresh \
  -H "X-Proxbox-API-Key: YOUR_KEY"
```

Each persisted version directory contains `openapi.json`, the offline
`pydantic_models.py` rendering, the raw capture, and `provenance.json`. The
provenance sidecar records `source_url`, `generated_at`, and the SHA-256 digest
of the exact `openapi.json` bytes. Runtime discovery ignores a user artifact
when the sidecar is missing or the digest differs. This sidecar detects
corruption; it does not authenticate the artifact, because a writer running as
the same operating-system user can replace the document and forge its digest.
This limitation is why production keeps runtime code generation disabled.

#### Regenerate an existing user schema

```bash
proxbox-schema generate 8.4 --force
```

Without `--force`, the command exits early when a user schema already exists.
Bundled tags cannot be regenerated or shadowed, even with `--force`; choose a
new tag instead.

#### Custom output directory

```bash
proxbox-schema generate 8.4 --output-dir /data/proxmox-schemas
```

Set `PROXBOX_GENERATED_DIR=/data/proxmox-schemas` and
`PROXBOX_RUNTIME_CODEGEN_ENABLED=true` for a development application when this
directory should be its discoverable user-generated schema root. Supplying
`--output-dir` alone does not change the running application's configured root.

#### Generate from a non-default source

```bash
proxbox-schema generate review-8.4 \
  --source-url https://schemas.example.net/api-viewer/ \
  --output-dir /data/proxmox-schemas
```

Artifacts from any non-default `source_url` are stored under
`/data/proxmox-schemas/custom/review-8.4/`. They are inspection-only and cannot
be discovered or registered as runtime proxy routes.

### Quarantine legacy artifacts

```bash
proxbox-schema quarantine-legacy
```

This idempotent upgrade step uses an interprocess lock and no-follow,
no-clobber moves to quarantine every `pydantic_models.py`, invalid runtime route
cache, and orphan cache provenance sidecar below the user-generated directory.
Application lifespan startup runs the same step before route registration.

#### Tune crawl performance

```bash
proxbox-schema generate 8.4 --workers 5 --retry-count 3 --retry-backoff 0.5
```

| Flag | Default | Description |
|------|---------|-------------|
| `--workers` | `10` | Number of async Playwright browser workers |
| `--retry-count` | `2` | Retries per endpoint on transient failures |
| `--retry-backoff` | `0.35` | Base exponential backoff in seconds |
| `--checkpoint-every` | `50` | Write a resume checkpoint every N endpoints |

Lower `--workers` if the crawl machine has limited resources. Increase `--retry-count` on flaky networks.

## HTTP API

The generation and refresh HTTP endpoints are development-only. They return
HTTP 404 because they are absent from the application route table unless the
process starts with `PROXBOX_RUNTIME_CODEGEN_ENABLED=true`.

### Check schema status

```http
GET /proxmox/viewer/schema-status
```

Response:

```json
{
  "available_versions": ["8.1", "8.2", "8.3", "latest"],
  "generation_tasks": {}
}
```

Check a specific version:

```http
GET /proxmox/viewer/schema-status?version_tag=8.4
```

Response when generation is in progress:

```json
{
  "version_tag": "8.4",
  "schema_available": false,
  "generation": {"status": "running", "error": null}
}
```

Possible `status` values: `pending`, `running`, `completed`, `failed`.

### Trigger generation

```http
POST /proxmox/viewer/generate?version_tag=8.4
```

This is a long-running synchronous request — it blocks until generation completes or fails. For background generation, prefer `proxbox-schema generate` or let auto-detection trigger it via `GET /proxmox/sessions`.

The `version_tag` query value must match
`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$` and cannot be `.` or `..`. Invalid values
return HTTP 422 before the crawler starts or any directory is created. The
pipeline also applies the same validation to direct Python and CLI callers and
resolves every checkpoint and generated artifact inside the configured output
directory.

When `persist=true`, a tag that exists in the package bundle returns HTTP 409
with instructions to choose a new tag or use `persist=false`. A non-default
`source_url` persists only under `custom/<version_tag>/`; the response identifies
the result as inspection-only.

### Refresh routes at runtime

After generating a new schema, register its routes without restarting:

```http
POST /proxmox/viewer/routes/refresh
```

Or for a specific version:

```http
POST /proxmox/viewer/routes/refresh?version_tag=8.4
```

## Requirements

Schema generation uses [Playwright](https://playwright.dev/python/) to headlessly crawl the Proxmox API Viewer. Install the extra:

```bash
pip install proxbox_api[playwright]
playwright install chromium
```

Without Playwright, the pipeline falls back to `apidoc.js` parsing. The fallback covers all endpoints but misses rendered descriptions from the interactive viewer.

## Version naming convention

Version tags use the `major.minor` format from the Proxmox `release` field (e.g. `"8.3"` from `{"release": "8.3", "version": "8.3.2"}`). The `latest` tag is a special alias for the most recent official API Viewer snapshot.

When a connected Proxmox cluster reports a release (e.g. `"8.3"`) that matches a bundled schema directory exactly, that schema is used. If no exact match is found, the app falls back to the highest same-major bundled version, then to `latest`.

## Runtime model loading

Runtime route registration builds request and response models directly from
the parsed OpenAPI document with `pydantic.create_model`. It does not evaluate
the generated `pydantic_models.py` source file. JSON property names and field
descriptions remain data supplied to Pydantic fields, including names that need
sanitized Python attributes while retaining their original JSON aliases.

`GET /proxmox/viewer/pydantic` renders source from a parsed, validated bundled
OpenAPI document by default. With the development opt-in, it may also render a
provenance-admitted user document. Rendering runs off the event loop, is cached
by the verified schema digest, is limited to 2 MiB of rendered source, and is
rate-limited to six requests per minute per source. It never reads a persisted
Python source file. The offline renderer validates class and field identifiers,
rejects normalized-name collisions and reserved Pydantic names, and uses Python
literal representations for aliases, descriptions, and defaults.

Before persistence, cache restoration, or model construction, the document is
bounded to 8 MiB, schema depth 32, 4,096 paths, 16,384 operations, 512
properties per schema, 8,192 generated models, and 4,096 characters for titles,
descriptions, and string enum values. A rejected cache never replaces an
already mounted last-known-good route set; startup falls back to authoritative
artifacts. Registration also rejects more than 8 eligible versions, more than
32 MiB of aggregate OpenAPI bytes, more than 16,384 aggregate models, or more
than 32,768 aggregate routes before model or cache construction.

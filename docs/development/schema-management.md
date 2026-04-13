# Proxmox Schema Management

`proxbox-api` ships bundled Proxmox OpenAPI schemas for the latest three stable PVE release lines. These schemas drive the runtime-generated proxy routes under `/proxmox/api2/*`. This page explains how to list, check, and generate schemas from the command line or via the HTTP API.

## Bundled schemas

The following versions are included with the package under `proxbox_api/generated/proxmox/`:

| Version tag | Proxmox release |
|-------------|-----------------|
| `8.1`       | PVE 8.1.x       |
| `8.2`       | PVE 8.2.x       |
| `8.3`       | PVE 8.3.x       |
| `latest`    | Current API Viewer snapshot |

On startup the app loads all available version directories and mounts routes for each. The startup log confirms which versions were found:

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

If the connected version has no matching bundled schema (for example, a future PVE 8.4 cluster), generation starts automatically in the background:

```json
{
  "schema_status": {
    "status": "generating",
    "version_tag": "8.4",
    "message": "No bundled schema found for Proxmox 8.4. Background generation started. This may take several minutes."
  }
}
```

Routes for the new version are registered at runtime once generation completes — no restart needed.

## CLI: `proxbox-schema`

The `proxbox-schema` command is the recommended way to manage schemas manually.

### List available versions

```bash
proxbox-schema list
```

Output:

```text
Available Proxmox OpenAPI schema versions (4):
         8.1   6.4 MB   /opt/proxbox_api/generated/proxmox/8.1/openapi.json
         8.2   6.4 MB   /opt/proxbox_api/generated/proxmox/8.2/openapi.json
         8.3   6.4 MB   /opt/proxbox_api/generated/proxmox/8.3/openapi.json
      latest   7.3 MB   /opt/proxbox_api/generated/proxmox/latest/openapi.json
```

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

This crawls the Proxmox API Viewer, parses all endpoints, and writes the generated artifacts under `proxbox_api/generated/proxmox/8.4/`. The command prints progress and a completion summary:

```
Generating Proxmox OpenAPI schema for version '8.4'...
Output directory: proxbox_api/generated/proxmox/8.4
Source URL: https://pve.proxmox.com/pve-docs/api-viewer/
Workers: 10

This may take several minutes. The pipeline crawls the Proxmox API Viewer,
parses all endpoints, and generates OpenAPI + Pydantic artifacts.

Generation completed for Proxmox 8.4
  Endpoints:  493
  Operations: 1284
  Duration:   187.3s
  Output:     proxbox_api/generated/proxmox/8.4

Schema is ready. Restart the app or call POST /proxmox/viewer/routes/refresh
to register the new routes at runtime.
```

After generation, register the new routes without restarting:

```bash
curl -s -X POST http://localhost:8800/proxmox/viewer/routes/refresh \
  -H "X-Proxbox-API-Key: YOUR_KEY"
```

#### Regenerate an existing schema

```bash
proxbox-schema generate 8.3 --force
```

Without `--force`, the command exits early when a schema already exists.

#### Custom output directory

```bash
proxbox-schema generate 8.4 --output-dir /data/proxmox-schemas
```

The app only loads schemas from `proxbox_api/generated/proxmox/` by default. Use a custom directory only if you mount it there or adjust the load path.

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

Use the HTTP API when you want to trigger generation or poll status from scripts or automation pipelines.

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

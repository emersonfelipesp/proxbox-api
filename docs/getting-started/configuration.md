# Configuration

`proxbox-api` uses SQLite for local bootstrap configuration and runtime dependencies.

## Database Location

- Default SQLite file: `database.db` in the repository root.
- ORM: SQLModel.
- Tables are created automatically at startup.

## NetBox Endpoint

NetBox endpoint configuration is managed with:

- `POST /netbox/endpoint`
- `GET /netbox/endpoint`
- `PUT /netbox/endpoint/{netbox_id}`
- `DELETE /netbox/endpoint/{netbox_id}`

Only one NetBox endpoint record is allowed.

The stored model now includes:

- `token_version`: `v1` or `v2`
- `token_key`: required for token v2, ignored for token v1
- `token`: the token secret
- `verify_ssl`: controls TLS certificate verification for all outbound NetBox HTTPS calls, including `ProxboxPluginSettings` runtime-settings fetches. Set this to `false` only for lab or private deployments that use self-signed certificates.

### NetBox token v1 example

```json
{
  "name": "netbox-primary",
  "ip_address": "10.0.0.20",
  "domain": "netbox.local",
  "port": 443,
  "token_version": "v1",
  "token": "<NETBOX_API_TOKEN>",
  "verify_ssl": true
}
```

### NetBox token v2 example

```json
{
  "name": "netbox-secondary",
  "ip_address": "10.0.0.21",
  "domain": "netbox.local",
  "port": 443,
  "token_version": "v2",
  "token_key": "token-name",
  "token": "<NETBOX_API_TOKEN_SECRET>",
  "verify_ssl": true
}
```

## Proxmox Endpoints

Proxmox endpoint records are managed with:

- `POST /proxmox/endpoints`
- `GET /proxmox/endpoints`
- `GET /proxmox/endpoints/{endpoint_id}`
- `PUT /proxmox/endpoints/{endpoint_id}`
- `DELETE /proxmox/endpoints/{endpoint_id}`

Authentication rules for create and update:

- Provide either `password`, or both `token_name` and `token_value`.
- `token_name` and `token_value` must be provided together.
- Endpoint names must be unique.

### Password-based example

```json
{
  "name": "pve-lab-1",
  "ip_address": "10.0.0.10",
  "domain": "pve-lab-1.local",
  "port": 8006,
  "username": "root@pam",
  "password": "<PASSWORD>",
  "verify_ssl": false
}
```

### Token-based example

```json
{
  "name": "pve-lab-token",
  "ip_address": "10.0.0.11",
  "domain": "pve-lab-token.local",
  "port": 8006,
  "username": "root@pam",
  "token_name": "api-token",
  "token_value": "<TOKEN_VALUE>",
  "verify_ssl": true
}
```

## Runtime Session Behavior

- NetBox sessions are derived from the single stored NetBox endpoint.
- The NetBox endpoint `verify_ssl` value is reused by plugin-settings fetches, so self-signed NetBox certificates work consistently when verification is disabled.
- Proxmox sessions default to local database endpoint records.
- Legacy source mode (`source=netbox`) is still supported in Proxmox session dependency behavior.

## Authentication

All API requests (except bootstrap endpoints) require authentication via the `X-Proxbox-API-Key` header. Keys are stored in the SQLite database with bcrypt hashing.

See [Authentication](./authentication.md) for complete documentation on:

- Bootstrap flow for first-time setup
- Key registration and management
- Auth-exempt endpoints
- Brute-force protection

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXBOX_NETBOX_TIMEOUT` | `120` | NetBox API client timeout in seconds. Applied to `netbox-sdk` config and underlying requests. |
| `PROXBOX_NETBOX_MAX_RETRIES` | `5` | Retry attempts for transient NetBox connection failures. |
| `PROXBOX_NETBOX_RETRY_DELAY` | `2.0` | Initial retry delay in seconds for NetBox retries. |
| `PROXBOX_NETBOX_MAX_CONCURRENT` | `1` | Maximum concurrent NetBox API requests. Keep low (1-2) to avoid exhausting NetBox's PostgreSQL connection pool. |
| `PROXBOX_VM_SYNC_MAX_CONCURRENCY` | `4` | Maximum number of concurrent VM sync write tasks. |
| `PROXBOX_NETBOX_WRITE_CONCURRENCY` | `8` (VM sync) / `4` (task-history, snapshots) | Maximum number of concurrent NetBox write operations. Default varies by sync service. |
| `PROXBOX_PROXMOX_FETCH_CONCURRENCY` | `8` (most paths) / `4` (task-history) | Maximum number of concurrent Proxmox read operations. Default varies by sync service. |
| `PROXBOX_FETCH_MAX_CONCURRENCY` | `8` | Legacy fetch concurrency override used by some sync entrypoints. |
| `PROXBOX_RATE_LIMIT` | `60` | Maximum API requests per minute per IP address. |
| `PROXBOX_BACKUP_BATCH_SIZE` | `5` | Backup sync batch size. Reduce to lower NetBox write pressure during backup sync. |
| `PROXBOX_BACKUP_BATCH_DELAY_MS` | `200` | Delay in milliseconds between backup batches. |
| `PROXBOX_BULK_BATCH_SIZE` | `50` | Per-batch size for bulk VM-related sync requests (volumes, backups). |
| `PROXBOX_BULK_BATCH_DELAY_MS` | `500` | Delay in milliseconds between bulk batches. |
| `PROXBOX_GENERATED_DIR` | `$XDG_DATA_HOME/proxbox/generated/proxmox` | Override output directory for the schema generator CLI (`proxbox-schema generate`). |
| `PROXBOX_CORS_EXTRA_ORIGINS` | (empty) | Comma-separated extra CORS origins added to the runtime allowlist. |
| `PROXBOX_EXPOSE_INTERNAL_ERRORS` | unset | When set to `1`, `true`, or `yes`, HTTP 500 responses include internal exception details. |
| `PROXBOX_STRICT_STARTUP` | unset | When set to `1`, `true`, or `yes`, startup fails if generated Proxmox routes cannot be mounted. |
| `PROXBOX_SKIP_NETBOX_BOOTSTRAP` | unset | When set to `1`, `true`, or `yes`, skips creating the default NetBox client during app startup. |
| `PROXBOX_ENCRYPTION_KEY` | unset | Secret key for encrypting credentials at rest. See [Credential Encryption](#credential-encryption) below. |

### Handling NetBox Overwhelmed Errors

When NetBox's PostgreSQL connection pool is saturated, proxbox-api returns `netbox_overwhelmed` errors. To mitigate:

1. **Reduce concurrency**: Set `PROXBOX_NETBOX_MAX_CONCURRENT=1` to serialize requests
2. **Increase retries**: More attempts with longer delays give NetBox time to recover
3. **Extend cache TTL**: Use `PROXBOX_NETBOX_GET_CACHE_TTL=300` to reduce redundant fetches

The retry logic applies aggressive backoff (up to 30 seconds) when overwhelmed errors are detected.

## CORS Behavior

- Origins are populated from NetBox endpoint records plus default development origins.
- Methods are currently allowed for all (`allow_methods=["*"]`).

## Credential Encryption

proxbox-api stores NetBox API tokens and Proxmox passwords/token values in a local SQLite database. When an encryption key is configured, these fields are encrypted at rest using **Fernet** (AES-128-CBC with HMAC-SHA256).

### Key resolution order

proxbox-api resolves the encryption key using the following priority chain:

1. **`PROXBOX_ENCRYPTION_KEY` environment variable** — highest priority, takes effect immediately on startup.
2. **`ProxboxPluginSettings.encryption_key`** — fetched from the NetBox plugin settings API (configurable on the `/plugins/proxbox/settings/` page in NetBox). Checked only if the env var is not set.
3. **None** — no key configured. Credentials are stored in plaintext and a `CRITICAL` warning is logged. Never use this in production.

### Setting the key

Generate a secure key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Then set it via environment variable:

```bash
export PROXBOX_ENCRYPTION_KEY="<paste key here>"
```

Or set it in the NetBox plugin settings page under **Encryption** → **Encryption key**.

### Backwards compatibility

If credentials were stored in plaintext before encryption was enabled, they continue to work — `decrypt_value` returns them unchanged when no `enc:` prefix is found. They are re-encrypted the next time the endpoint is saved.

If the encryption key changes after credentials were already encrypted, proxbox-api logs a warning and returns the raw ciphertext (unusable as a credential). Re-save each endpoint with the correct credentials after rotating the key.

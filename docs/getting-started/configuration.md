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
- Proxmox sessions default to local database endpoint records.
- Legacy source mode (`source=netbox`) is still supported in Proxmox session dependency behavior.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXBOX_NETBOX_TIMEOUT` | `120` | NetBox API client timeout in seconds. Applied to `netbox-sdk` config and underlying requests. |
| `PROXBOX_NETBOX_MAX_RETRIES` | `3` | Retry attempts for transient NetBox connection failures. |
| `PROXBOX_NETBOX_RETRY_DELAY` | `1.0` | Initial retry delay in seconds for NetBox retries. |
| `PROXBOX_VM_SYNC_MAX_CONCURRENCY` | `4` | Maximum number of concurrent VM sync write tasks. |
| `PROXBOX_NETBOX_WRITE_CONCURRENCY` | `8` | Maximum number of concurrent NetBox write operations. |
| `PROXBOX_PROXMOX_FETCH_CONCURRENCY` | `8` | Maximum number of concurrent Proxmox read operations. |
| `PROXBOX_FETCH_MAX_CONCURRENCY` | `8` | Legacy fetch concurrency override used by some sync entrypoints. |
| `PROXBOX_CORS_EXTRA_ORIGINS` | (empty) | Comma-separated extra CORS origins added to the runtime allowlist. |
| `PROXBOX_EXPOSE_INTERNAL_ERRORS` | unset | When set to `1`, `true`, or `yes`, HTTP 500 responses include internal exception details. |
| `PROXBOX_STRICT_STARTUP` | unset | When set to `1`, `true`, or `yes`, startup fails if generated Proxmox routes cannot be mounted. |
| `PROXBOX_SKIP_NETBOX_BOOTSTRAP` | unset | When set to `1`, `true`, or `yes`, skips creating the default NetBox client during app startup. |

## CORS Behavior

- Origins are populated from NetBox endpoint records plus default development origins.
- Methods are currently allowed for all (`allow_methods=["*"]`).

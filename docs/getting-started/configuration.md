# Configuration

`proxbox-api` uses SQLite for local bootstrap configuration and runtime dependencies.

## Database location

- Default SQLite file: `database.db` in repository root.
- ORM: SQLModel.
- Tables are created automatically at startup.

## NetBox endpoint (singleton)

NetBox endpoint configuration is managed with:

- `POST /netbox/endpoint`
- `GET /netbox/endpoint`
- `PUT /netbox/endpoint/{netbox_id}`
- `DELETE /netbox/endpoint/{netbox_id}`

Only one NetBox endpoint record is allowed.

### Example payload

```json
{
  "name": "netbox-primary",
  "ip_address": "10.0.0.20",
  "domain": "netbox.local",
  "port": 443,
  "token": "<NETBOX_API_TOKEN>",
  "verify_ssl": true
}
```

## Proxmox endpoints (multiple)

Proxmox endpoint records are managed with:

- `POST /proxmox/endpoints`
- `GET /proxmox/endpoints`
- `GET /proxmox/endpoints/{endpoint_id}`
- `PUT /proxmox/endpoints/{endpoint_id}`
- `DELETE /proxmox/endpoints/{endpoint_id}`

Authentication rules for create and update:

- You must provide either `password`, or both `token_name` and `token_value`.
- `token_name` and `token_value` must be provided together.

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

## Runtime session behavior

- NetBox session is derived from the single stored NetBox endpoint.
- Proxmox sessions default to local database endpoint records.
- Legacy source mode (`source=netbox`) is still supported in Proxmox session dependency behavior.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXBOX_NETBOX_TIMEOUT` | `120` | NetBox API client timeout in seconds. Applied to `netbox-sdk` config and underlying `aiohttp` requests. Increase if NetBox responses are slow under load. |
| `PROXBOX_VM_SYNC_MAX_CONCURRENCY` | `4` | Maximum number of concurrent VM creation tasks during synchronization. Uses an `asyncio.Semaphore` to limit parallel NetBox API load. Lower values reduce timeout risk; higher values increase throughput on fast backends. |
| `PROXBOX_FETCH_MAX_CONCURRENCY` | `8` | Maximum number of concurrent fetch operations used in backup, snapshot, and storage sync flows. |
| `PROXBOX_CORS_EXTRA_ORIGINS` | (empty) | Comma-separated extra CORS origins added to the runtime allowlist. |
| `PROXBOX_EXPOSE_INTERNAL_ERRORS` | unset | When set to `1`, `true`, or `yes`, HTTP 500 responses include internal exception details. By default, details are hidden from API clients and logged server-side. |
| `PROXBOX_STRICT_STARTUP` | unset | When set to `1`, `true`, or `yes`, startup fails if generated Proxmox routes cannot be mounted. Default behavior logs a warning and continues. |
| `PROXBOX_SKIP_NETBOX_BOOTSTRAP` | unset | When set to `1`, `true`, or `yes`, skips creating the default NetBox client during app startup. Useful for local development or partial startup workflows. |

## CORS behavior

- Origins are populated from NetBox endpoint records plus default development origins.
- Methods are currently allowed for all (`allow_methods=["*"]`).

# Large Multi-Cluster Deployments

Large estates usually need longer transport budgets and a higher ingress rate
limit, not higher concurrency. The netbox-proxbox plugin invokes most sync
stages with one Proxmox endpoint at a time, so per-endpoint concurrency does not
need to grow with the number of clusters. Inside proxbox-api, backup-routine and
replication collection still fan out one Proxmox task per supplied session
concurrently (`backup_routines.py`, `replications.py`), and firewall or
datacenter aggregate reads traverse every session you pass in. Size Proxmox and
NetBox capacity for that endpoint-scoped orchestration plus any multi-session
fan-out your jobs include. Increase concurrency only after measuring the
Proxmox, NetBox, and PostgreSQL capacity available to one job.

## Runtime tunables

Where listed, environment variables override values from the NetBox Proxbox
settings page. Plugin values are cached for five minutes; environment changes
require a process restart. Proxmox transport values have no environment
override: they can be overridden on each Proxmox endpoint, and the endpoint
value wins over the plugin default.

| Environment variable | Plugin setting | Default | Minimum | Controls |
|---|---|---:|---:|---|
| — | `proxmox_timeout` | 5 s | 1 | Default Proxmox HTTP timeout; each endpoint can override it. |
| — | `proxmox_max_retries` | 0 | 0 | Default Proxmox retry count; each endpoint can override it. |
| — | `proxmox_retry_backoff` | 0.5 s | 0 | Default Proxmox retry backoff; each endpoint can override it. |
| `PROXBOX_NETBOX_TIMEOUT` | `netbox_timeout` | 120 s | 1 | Total timeout for a NetBox HTTP request. |
| `PROXBOX_NETBOX_MAX_RETRIES` | `netbox_max_retries` | 5 | 0 | Retries for transient NetBox transport failures. |
| `PROXBOX_NETBOX_RETRY_DELAY` | `netbox_retry_delay` | 2.0 s | 0 | Base delay for exponential NetBox retry backoff. |
| `PROXBOX_NETBOX_MAX_CONCURRENT` | `netbox_max_concurrent` | 1 | 1 | Concurrent NetBox REST requests. Keep this within the NetBox database connection-pool budget. |
| `PROXBOX_NETBOX_WRITE_CONCURRENCY` | `netbox_write_concurrency` | 8 | 1 | Concurrent write-heavy per-VM operations inside a sync pass. |
| `PROXBOX_PROXMOX_FETCH_CONCURRENCY` | `proxmox_fetch_concurrency` | 8 | 1 | Concurrent Proxmox reads in interface, snapshot, backup, task-history, and related stages. |
| `PROXBOX_VM_SYNC_MAX_CONCURRENCY` | `vm_sync_max_concurrency` | 4 | 1 | Concurrent VM configuration fetches and virtual-disk operations. |
| `PROXBOX_BULK_BATCH_SIZE` | `bulk_batch_size` | 50 | 1 | Objects per general NetBox bulk-write batch. |
| `PROXBOX_BULK_BATCH_DELAY_MS` | `bulk_batch_delay_ms` | 500 ms | 0 | Delay between general bulk-write batches. |
| `PROXBOX_BACKUP_BATCH_SIZE` | `backup_batch_size` | 5 | 1 | VMs per backup discovery and reconciliation batch. |
| `PROXBOX_BACKUP_BATCH_DELAY_MS` | `backup_batch_delay_ms` | 200 ms | 0 | Delay between backup batches. |
| `PROXBOX_INTERFACE_BATCH_SIZE` | `interface_batch_size` | 5 | 1 | VMs per interface synchronization batch. |
| `PROXBOX_INTERFACE_BATCH_DELAY_MS` | `interface_batch_delay_ms` | 100 ms | 0 | Delay between interface batches. |
| `PROXBOX_GUEST_AGENT_TIMEOUT` | — | 15.0 s | 1.0 | Timeout for one QEMU guest-agent `network-get-interfaces` call. Environment-only (no NetBox plugin setting); restart required after change. |

See [Runtime Concurrency Tunables](../development/async-tunables.md) for the
implementation-oriented concurrency and diagnostic reference.

## Starting profile for about 30 clusters

Use this as a conservative baseline, then tune from measurements:

```text
PROXBOX_RATE_LIMIT=3000
proxmox_timeout=15
proxmox_max_retries=2
proxmox_retry_backoff=1.0
netbox_timeout=180
netbox_max_concurrent=1
```

For remote clusters, set the endpoint-specific Proxmox timeout to 20-30
seconds. Use `netbox_max_concurrent=2` only when the NetBox database pool has
capacity for it. Leave the concurrency and batch settings at their defaults
initially.

## Rate limiting and authentication cost

`PROXBOX_RATE_LIMIT` is a process-level per-source-address request limit with a
default of 300 requests per minute. The middleware runs before authentication.
All requests from NetBox commonly reach proxbox-api from one address, so a
large synchronization and normal UI traffic share one allowance. A value of
`3000` is a practical starting point for a large estate.

An exhausted allowance returns HTTP 429 with:

```json
{"detail":"Rate limit exceeded. Please try again later."}
```

Keep one active proxbox-api key when possible. Authentication checks active
keys in order, and each candidate adds a bcrypt verification to every
authenticated request. Rotate the key, confirm the replacement works, and then
deactivate the old key.

## Process and job layout

Uvicorn worker count is a tradeoff, not a hard limit of one:

- **Rate limiting** — `PROXBOX_RATE_LIMIT` is enforced per worker process. Each
  worker maintains its own per-source-address budget, so multiple workers
  multiply effective ingress capacity but also split the global allowance unless
  you tune the limit accordingly.
- **Active sync registry** — the advisory in-memory view of running syncs is per
  worker. `GET /sync/active` and related status only reflect jobs on the worker
  that serves the request unless you standardize on one worker or treat the
  probe as approximate.
- **Interactive execution policy** — `PROXBOX_EXECUTION_MODE` and related
  generation pins are process-local; mixed workers can disagree on interactive
  admission unless every worker shares the same configuration.
- **Browser console relay** — standalone relay tickets and Fernet payloads live
  in shared SQLite, so create and consume can succeed across workers when the
  database is shared.
- **NetBox connection pressure** — each worker applies `netbox_max_concurrent`
  independently. Total concurrent NetBox REST usage scales roughly with
  `netbox_max_concurrent × workers`.

For globally coherent rate-limit and sync-status semantics, one worker plus a
higher `PROXBOX_RATE_LIMIT` is usually the simplest layout. Multiple workers can
be appropriate when you accept partitioned limits and sync visibility, size
NetBox pools for the multiplied concurrency, and keep interactive policy aligned
on every process. Workers are not a substitute for raising sync-stage
concurrency tunables inside a single job.

Avoid reloading the Proxbox home page while a sync is starting because its
initial API requests compete with the sync burst for the same source-address
allowance. Split very large estates into endpoint subsets and run one subset per
job. This limits the failure scope and makes duration and downstream load
easier to measure.

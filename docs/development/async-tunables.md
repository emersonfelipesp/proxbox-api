# Runtime Concurrency Tunables

## Settings Resolution Order

Every async-related tunable in proxbox-api resolves through a three-level
priority chain with a 5-minute cache:

```
env var  >  ProxboxPluginSettings (NetBox plugin page)  >  built-in default
```

The resolution is handled by `proxbox_api.runtime_settings.get_int` (and
`get_float`, `get_bool`, `get_str`):

```python
def get_int(
    settings_key: str,
    env: str,
    default: int,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    # 1. check os.environ
    if env in os.environ:
        return clamp(int(os.environ[env]), minimum, maximum)
    # 2. check ProxboxPluginSettings (cached 5 min)
    settings = _get_cached_plugin_settings()
    if settings and hasattr(settings, settings_key):
        return clamp(getattr(settings, settings_key) or default, minimum, maximum)
    # 3. built-in default
    return default
```

This means you can change concurrency without restarting the service — update
the plugin settings page in NetBox and the new value will take effect within
5 minutes (or immediately on restart).

## Concurrency Tunables Reference

| Env Var | Plugin Settings Key | Default | Min | Description |
|---|---|---|---|---|
| `PROXBOX_VM_SYNC_MAX_CONCURRENCY` | `vm_sync_max_concurrency` | 4 | 1 | Max concurrent Proxmox VM config fetches in the VM and virtual-disk sync phases |
| `PROXBOX_VM_CONFIG_FETCH_TIMEOUT_SECONDS` | — | 30 | 1 | Wall-clock seconds allowed for one full-sync VM config fetch before that VM is isolated as failed |
| `PROXBOX_NETBOX_WRITE_CONCURRENCY` | `netbox_write_concurrency` | 8 | 1 | Max concurrent NetBox API write-heavy per-VM sync tasks (VMs and virtual disks) |
| `PROXBOX_PROXMOX_FETCH_CONCURRENCY` | `proxmox_fetch_concurrency` | 8 | 1 | Max concurrent Proxmox API reads for interfaces |
| `PROXBOX_SESSION_ACQUIRE_CONCURRENCY` | `session_acquire_concurrency` | 8 | 1 | Max concurrent session opens for partial multi-endpoint acquisition |
| `PROXBOX_INTERFACE_BATCH_SIZE` | `interface_batch_size` | 5 | 1 | VMs per interface-sync batch (prevents NetBox overload) |
| `PROXBOX_INTERFACE_BATCH_DELAY_MS` | `interface_batch_delay_ms` | 100 | 0 | Milliseconds between interface-sync batches |
| `PROXBOX_GUEST_AGENT_TIMEOUT` | — | 15.0 | 1.0 | Seconds for guest-agent `network-get-interfaces` call (environment-only; not on the NetBox plugin settings page) |
| `PROXBOX_NETBOX_MAX_CONCURRENT` | `netbox_max_concurrent` | 1 | 1 | Max concurrent NetBox GET requests (keep low to avoid PostgreSQL pool exhaustion) |
| `PROXBOX_NETBOX_TIMEOUT` | `netbox_timeout` | 120 | 1 | NetBox HTTP session total timeout in seconds |

For estate-wide sizing, ingress rate limits, worker topology, and a starting
profile for about 30 clusters, see [Large Multi-Cluster Deployments](../operations/large-multi-cluster-deployments.md).

`ProxmoxSessionsDep` remains strict: one failed endpoint fails the dependency
and closes sessions already opened for an ordinary inventory request.
`ProxmoxSessionsPartialDep` is an explicit opt-in for multi-endpoint aggregate
reads. It returns data from healthy endpoints and reports secret-safe failures
in the bounded JSON `X-Proxbox-Endpoint-Errors` response header. The summary has
the shape `{"failed": <count>, "ids": [<up to 20 endpoint IDs>], "truncated":
<boolean>}`, is capped at 1024 bytes, and contains no endpoint names or error
messages. If every selected endpoint fails, the request still returns an error
response.

During synchronization, the netbox-proxbox plugin requests firewall and
datacenter data one endpoint at a time, so an endpoint failure is reported as
that endpoint's failure. Other multi-endpoint callers can use the bounded
summary header to identify failed endpoint IDs while retaining healthy data.

## The Single `netbox_version` Optimization (F3)

Before this optimization, `ensure_vm_type` called `detect_netbox_version(nb)` on
every invocation — one extra NetBox round-trip per VM type per sync pass. On a
cluster with 50 unique VM types and 500 VMs this could mean 50+ redundant
version checks per sync.

The fix calls `detect_netbox_version` **once** at the start of
`create_virtual_machines` and threads the result to every `ensure_vm_type` call:

```python
# Called once at the beginning of the sync pass
netbox_version = await asyncio.to_thread(detect_netbox_version, nb)

# Threaded through all ensure_vm_type calls
for vm_type in unique_vm_types:
    await ensure_vm_type(nb, vm_type=vm_type, tag_refs=tag_refs,
                         netbox_version=netbox_version)   # <-- pre-resolved
```

`detect_netbox_version` is a blocking function (it calls the NetBox status API
synchronously), so it is also wrapped in `asyncio.to_thread` for event-loop
safety.

## Diagnosing Concurrency Bottlenecks

### Identify the Bottleneck from Timing Logs

The `_run_full_update_vm_batch` function logs phase durations:

```
VM full-update phase timing: fetch_ms=8234.12 process_ms=432.10 fetched_ok=480 fetch_failed=2
```

| Condition | Likely cause | Tuning action |
|---|---|---|
| `fetch_ms` very high, `fetched_ok` low | Proxmox API is slow or `PROXBOX_VM_SYNC_MAX_CONCURRENCY` too low | Raise concurrency (check Proxmox rate limits first) |
| `fetch_ms` high + many `fetch_failed` | Proxmox API is overloaded | Lower concurrency or add rate limiting |
| `process_ms` high | Preparation is slow; it includes dependency resolution and local model work | Separate upstream waits from CPU profiles before tuning; profile `build_netbox_virtual_machine_payload` only when CPU evidence points there |
| NetBox write timeouts in dispatch | PostgreSQL pool exhaustion | Lower `PROXBOX_NETBOX_WRITE_CONCURRENCY` |

The detailed batch timing log adds `process_cpu_ms`, `snapshot_ms`,
`hydration_ms`, `name_resolution_ms`, `canonicalization_ms`, `dispatch_ms`, and
`persistence_ms`. Compare `total_ms` with process CPU time before calling a
stage CPU-bound. `process_cpu_ms` is process-wide and is attributable to this
sync only during an isolated run. The same log records config request count,
total/max config latency, effective worker/queue limits, and process peak RSS.
The config-fetch worker set has a pending queue capacity of
twice `PROXBOX_VM_SYNC_MAX_CONCURRENCY`; increasing the setting raises both
active upstream requests and bounded pending work. Tune it only after request
counts, pool waits, timeout rates, and upstream pressure are measured.

### Check for Guest-Agent Stalls

If `PROXBOX_GUEST_AGENT_TIMEOUT` is too low for VMs with many interfaces, the
guest-agent data is silently dropped. Look for:

```
WARNING proxbox_api: Guest agent timeout (attempt 1): vmid=101 node=pve01 timeout=15.0s
```

Raise `PROXBOX_GUEST_AGENT_TIMEOUT` to `30` or `60` for VRRP router VMs. If the
timeout is already at its maximum and the call still times out, the VM guest
agent may be overloaded or misconfigured.

### Use the Cache Debug Log

Set `PROXBOX_DEBUG_CACHE=true` to emit per-request cache hit/miss/evict events
from the NetBox GET cache. High miss rates increase effective NetBox latency and
can be addressed by tuning `PROXBOX_NETBOX_GET_CACHE_TTL` or
`PROXBOX_NETBOX_GET_CACHE_MAX_ENTRIES`.

## Example: Tuning for a Large Cluster

For a cluster with 2000 VMs on a low-latency host:

```bash
# .env or docker-compose environment section
PROXBOX_VM_SYNC_MAX_CONCURRENCY=8      # up from 4 — cluster can handle it
PROXBOX_NETBOX_WRITE_CONCURRENCY=12    # up from 8 — DB pool supports it
PROXBOX_PROXMOX_FETCH_CONCURRENCY=12  # up from 8 — interface reads are fast
PROXBOX_GUEST_AGENT_TIMEOUT=30        # up from 15 — some VMs are slow
PROXBOX_NETBOX_GET_CACHE_TTL=120      # up from 60 — reduce GET pressure
```

Monitor the timing logs after each change. Do not raise concurrency beyond what
the downstream service can handle — PostgreSQL connection pool exhaustion is
harder to diagnose than slow throughput.

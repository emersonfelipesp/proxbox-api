# VM Reconciliation Architecture

This page documents the VM full-sync reconciliation architecture used by `proxbox-api` when VM sync runs in full-update mode (`sync_vm_network=false`).

The design goal is to maximize read throughput from Proxmox while keeping NetBox write pressure low and deterministic.

## Why This Architecture Exists

The old model mixed fetch/reconcile/write per VM and could interleave many NetBox writes while still discovering data.

The new model separates the workflow into explicit phases:

1. Read all required Proxmox VM inputs into memory (parallel).
2. Read all required NetBox VM state into memory (single snapshot pass).
3. Compare desired vs current with Pydantic-normalized payloads.
4. Build a deterministic operation queue (`GET`, `CREATE`, `UPDATE`).
5. Dispatch operations sequentially in batch windows controlled by global configuration.

This keeps NetBox updates controlled for a single shared NetBox instance while still allowing parallel Proxmox data collection.

## Execution Phases

### Phase 1: Dependency Preflight

Before reconciling VMs, the sync ensures parent dependencies in NetBox:

- Manufacturer
- Device type
- Proxmox node role
- Cluster type
- Cluster
- Site
- Node device
- VM role (QEMU/LXC)

These are required for deterministic VM payload generation.

### Phase 2: Proxmox Read Snapshot (Parallel)

For each VM candidate, sync prepares an in-memory state object containing:

- Cluster + VM identity
- Proxmox VM resource record
- Proxmox VM config
- Normalized desired NetBox VM payload
- NetBox lookup keys (cluster + `cf_proxmox_vm_id`)

Preparation runs with bounded concurrency using `asyncio.gather` + semaphore.

### Phase 3: NetBox Read Snapshot (In-Memory)

The sync reads all NetBox VMs in paginated batches (`limit/offset`) and builds an in-memory index keyed by:

- `(cluster_id, proxmox_vm_id)`

This avoids repeated NetBox list/filter calls during per-VM comparison.

### Phase 4: Pydantic Reconciliation

For each prepared VM:

1. Validate desired payload with `NetBoxVirtualMachineCreateBody`.
2. Normalize current NetBox record with the same schema.
3. Compute delta fields.

Classification result:

- `GET`: Object exists and no delta.
- `CREATE`: Object missing in snapshot index.
- `UPDATE`: Object exists and delta is non-empty.

### Phase 5: Sequential NetBox Dispatch in Batch Windows

Operations are executed in deterministic queue order.

- Batch window size is read from `PROXBOX_NETBOX_WRITE_CONCURRENCY`.
- Inside each batch window, writes are still executed one-by-one (sequential).
- `GET` operations are no-op reads from in-memory state.
- `CREATE` operations call NetBox create.
- `UPDATE` operations call NetBox patch by record ID.

After object reconciliation, VM task-history sync is executed per resolved VM record.

## Mermaid Diagrams

### End-to-End Reconciliation Flow

```mermaid
flowchart TD
    A[Start VM Full-Sync] --> B[Dependency Preflight]
    B --> C[Prepare VM States from Proxmox in Parallel]
    C --> D[Load NetBox VM Snapshot in Memory]
    D --> E[Pydantic Compare Desired vs Current]
    E --> F[Build Ordered Queue: GET CREATE UPDATE]
    F --> G[Dispatch Queue in Batch Windows]
    G --> H[Sequential NetBox Write Execution]
    H --> I[Sync VM Task History]
    I --> J[Return Reconciled VM Results]
```

### Parallel Read + Sequential Write Model

```mermaid
sequenceDiagram
    participant P1 as Proxmox Endpoint A
    participant P2 as Proxmox Endpoint B
    participant S as Sync Engine
    participant N as NetBox API

    par Parallel Proxmox Reads
        S->>P1: Fetch resources/configs
        P1-->>S: VM data snapshot A
    and
        S->>P2: Fetch resources/configs
        P2-->>S: VM data snapshot B
    end

    S->>N: Fetch NetBox VM pages
    N-->>S: NetBox in-memory snapshot

    S->>S: Reconcile with Pydantic
    S->>S: Build ordered operations queue

    loop Sequential batch dispatch
        S->>N: CREATE or UPDATE (one at a time)
        N-->>S: Response
    end
```

## Operation Semantics

`GET`

- Means no NetBox write is required.
- Record is reused from in-memory NetBox snapshot.

`CREATE`

- Means no matching `(cluster_id, proxmox_vm_id)` was found.
- NetBox POST is executed once during dispatch.

`UPDATE`

- Means object exists but reconciled payload differs.
- NetBox PATCH includes only changed fields.

## Configuration and Tuning

- `PROXBOX_VM_SYNC_MAX_CONCURRENCY`: controls concurrent VM preparation/fetch from Proxmox.
- `PROXBOX_NETBOX_WRITE_CONCURRENCY`: defines dispatch batch window size.

Note: batch window size does not imply parallel writes; writes remain sequential to protect NetBox under single-instance operation.

## Benefits

- Predictable NetBox write load.
- Reduced duplicate or conflicting updates.
- Clear phase boundaries for troubleshooting.
- Better observability of planned vs executed operations.
- Easier future extension for dry-run/export of operation plans.

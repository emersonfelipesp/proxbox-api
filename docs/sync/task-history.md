# Task History Synchronization

Task-history synchronization copies terminal Proxmox VE archive rows into the
netbox-proxbox task-history model. The backend owns collection, identity
resolution, deduplication, and reconciliation; callers only choose whether the
VM stage should run that work or leave it to a dedicated full-update stage.

## Collection and reconciliation model

The bulk service uses bounded, node-oriented collection:

1. Load the relevant NetBox VM records. A selected run deduplicates and sorts
   IDs, sends at most 100 IDs per request, and encodes NetBox's multi-value
   filter as repeated values (`?id=1&id=2`) instead of comma text. Results are
   deduplicated across chunks, so selected work stays bounded without reading
   the estate or exceeding common request-line limits.
2. Load the VM sync-state sidecar once, join it by `virtual_machine`, and match
   archive rows in memory by `(proxmox_endpoint_raw_id, proxmox_cluster_name,
   proxmox_vm_id)`. Guest type is required sidecar identity evidence and is
   written to the reconciled row, but it is neither an ownership-matching key
   nor a Proxmox archive query parameter: if two VMs ever claim the same
   endpoint, cluster, and VMID, the UPID is skipped as ambiguous rather than
   split by guest type.
3. Select only the endpoint/cluster nodes that own those VMs.
4. Walk each selected node's `source=archive` task list once, using `limit=500`,
   increasing `start` offsets, and one fixed run-start `until` timestamp.
5. Associate archive rows to VMs in memory, deduplicate by UPID, and reconcile
   all payloads with one bulk NetBox operation.

All archive page requests share one
`PROXBOX_PROXMOX_FETCH_CONCURRENCY` semaphore. There is no arbitrary maximum
page count: Proxmox rotates its task archive, so synchronization walks the
available archive until a short/empty page. Repeated-page and no-new-UPID guards
stop safely if an endpoint ignores pagination and mark the run degraded. NetBox
list reads follow the server-provided `next` links, so a server page-size cap
cannot truncate the snapshot; repeated links or page content fail closed rather
than returning a partial set that could provoke duplicate creates.

Archive rows already contain terminal `status` and `endtime`. The service
mirrors that archive status into NetBox `status` and `exitstatus`, and writes
`task_state="stopped"`; it does not issue a status request for every UPID.
Likewise, bulk create/patch failure is not converted into per-record NetBox
requests. This keeps request growth proportional to nodes and pages rather than
`VMs × nodes × tasks`.

## Identity safety

The typed VM sync-state sidecar is authoritative for VMID, raw endpoint ID, VM
type, and Proxmox cluster name. A malformed or duplicate sidecar belonging to a
VM relevant to the run fails closed and is never masked by custom fields; a
selected request is not aborted by corrupt sidecars that belong only to an
unrelated VM. Legacy custom-field identity is considered only when no sidecar
row exists (or the optional sidecar route cannot be read) and
`custom_fields_enabled=true`.

When an estate-wide sidecar scan succeeds, a NetBox VM with neither a sidecar
nor usable legacy identity is treated as unmanaged and skipped. An explicitly
selected VM without identity remains fatal. An unavailable/transient sidecar
read with custom fields disabled is also fatal because ownership cannot be
verified. A task from endpoint 11 therefore cannot fall back to a VM explicitly
pinned to endpoint 22. Legacy `(cluster_name, vmid)` matching additionally
requires a globally unique VM match and one source endpoint/session for that
cluster name. True exact/legacy ownership collisions and duplicate cluster
sources are skipped and mark the run degraded; unrelated archive VMIDs are
ordinary skips and do not count as errors.

One UPID observed on old and new nodes for the same VM (for example after
migration) is deduplicated safely. The same UPID resolving to different VM
owners is ambiguous, is skipped, and marks the run degraded. Existing NetBox
rows with a wrong VM or VM type are repaired because `virtual_machine` and
`vm_type` are patchable fields.

## API ownership and compatibility

The VM create routes and both SSE variants expose
`sync_task_history: bool = true`, including the targeted
`/{netbox_vm_id}/create` routes:

- Omitted or `true`: preserve standalone legacy behavior and run one scoped
  aggregate after all successfully reconciled VMs are known. A fatal owned
  aggregate propagates through REST/SSE; the VM route cannot silently succeed.
  If collection reconciles rows but returns `degraded=true`, standalone REST
  raises HTTP 502 after retaining those rows. SSE retains its warning phase
  summary so partial coverage is visible in the stream.
- `false`: skip task history in the VM stage because another stage owns it.

`/full-update` and `/full-update/stream` explicitly call the VM stage with
`sync_task_history=false`, then run the dedicated all-VM task-history stage once.
A selected VM batch passes only successfully reconciled NetBox VM IDs to the
aggregate, so unrelated endpoints are not queried. Its preliminary NetBox VM
lookup uses the same bounded repeated-value chunks and fails closed if any
chunk cannot be read. A one-VM targeted reconcile
still scans the task-history table exactly once: the NetBox task-history schema
uses UPID as its global lookup key, and a VM filter would hide an existing UPID
attached to the wrong VM and prevent the self-healing patch. Node collection
remains restricted to the selected scope.

The dedicated `/virtualization/virtual-machines/task-history/create/stream`
route accepts the same comma-separated `netbox_vm_ids` scope. Omission selects
the estate; an explicitly empty, malformed, or non-positive value is rejected
with ordinary HTTP 422 before the SSE response begins and can never widen into
an estate-wide run. The route accepts only
`fetch_max_concurrency >= 1` and resets the optional-sidecar availability memo
at the start of every request so an earlier 404/501 is re-probed.

The response field named `created` is retained for compatibility but represents
the total number of task-history rows reconciled in that run: created, updated,
or already unchanged.

Deploy the backend support first, then switch an orchestrating plugin to send
`sync_task_history=false`. Older plugins remain compatible because omission
still defaults to `true`. Older backends ignore an unknown query parameter, so
deploying the plugin first can temporarily retain the old duplicate work even
though records remain idempotent.

## Degraded runs

A later-page or single-node failure, repeated archive page, no-new-UPID page,
partially missing endpoint/cluster node coverage, ownership ambiguity, or
cross-owner UPID retains safe rows already collected, then returns
`degraded=true` with an `errors` count. Every requested target scope is compared
with the discovered sessions/statuses and must have at least one node; if some
scopes are absent the available scopes continue degraded, while absence of all
requested scopes is fatal.
The SSE phase summary reports that count as `failed`, so incomplete coverage is
visible even though the partial stage completes and the collected rows persist.
The next run supplies eventual consistency for the missing tail. VM-list
failure, no usable selected nodes, failure of every selected node, or global
bulk-reconcile failure raises `ProxboxException`; REST/SSE therefore reports a
failed stage with `complete.ok=false` instead of a misleading successful zero.
Cancellation propagates immediately and no NetBox reconciliation is attempted.

Operators should treat `degraded=true` as a retry/inspection signal and review
the node, offset, and retained-row warning in backend logs. A normal empty
archive is not degraded.

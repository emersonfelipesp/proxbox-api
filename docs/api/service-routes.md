# Service Route Groups

This page documents the authored FastAPI route groups that sit outside the
generated Proxmox API viewer surface. Use the runtime OpenAPI at `/docs` for
full request and response schemas.

## Mounting Model

`proxbox-api` always mounts root metadata and `/auth` bootstrap/key-management
routes. The rest of the route surface depends on `PROXBOX_FEATURES`:

| `PROXBOX_FEATURES` value | Mounted route surface |
|---|---|
| unset or empty | Core Proxmox, NetBox, sync, cloud, intent, WebSocket, cache, admin, plus optional PBS/Ceph/PDM route groups when their packages import successfully |
| subset of `pbs,ceph,pdm` | Sidecar-only mode: only the listed optional service groups are mounted, along with root metadata and auth |

Optional service imports fail open. If the PBS, Ceph, or PDM subpackage is not
available, the corresponding route group is skipped and the app logs the reason.

All non-bootstrap HTTP routes require `X-Proxbox-API-Key`. Write-capable
Proxmox, cloud, and intent routes also depend on the relevant
`ProxmoxEndpoint.allow_writes` or route-specific execution flag.

## Route Group Index

| Prefix | Purpose |
|---|---|
| `/cloud` | NMS Cloud VM, LXC, template, image-factory, Azure VHD import, and Firecracker provisioning workflows |
| `/intent` | NetBox-to-Proxmox intent planning, apply, deletion-request approval chain, and pending-deletion tag helpers |
| `/pbs` | Proxmox Backup Server endpoint configuration, status probes, and read-only sync summaries |
| `/pdm` | Proxmox Datacenter Manager endpoint configuration, status probes, and read-only sync summaries |
| `/ceph` | Read-only Proxmox-managed Ceph status and sync summary routes |
| `/ceph/v2` | Desired-state Ceph plan/apply/reconcile surface used by `netbox-ceph` |
| `/ssh` | Short-lived SSH terminal sessions and WebSocket transport |

## PBS (`/pbs`)

PBS routes operate on locally stored `PBSEndpoint` records. The sync routes
fetch data and return summary envelopes; v1 does not issue NetBox writes. Each
sync route accepts optional `netbox_branch_schema_id` so branch-aware writes can
reuse the same surface when persistence is added.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/pbs/endpoints` | Create a PBS endpoint record |
| `GET` | `/pbs/endpoints` | List configured PBS endpoints |
| `GET` | `/pbs/endpoints/{endpoint_id}` | Read one PBS endpoint |
| `PUT` | `/pbs/endpoints/{endpoint_id}` | Update one PBS endpoint |
| `DELETE` | `/pbs/endpoints/{endpoint_id}` | Delete one PBS endpoint |
| `GET` | `/pbs/status` | Probe reachability and version for enabled endpoints |
| `GET` | `/pbs/sync/full` | Fetch datastore, snapshot, job, and node summaries |
| `GET` | `/pbs/sync/datastores` | Fetch datastore summaries |
| `GET` | `/pbs/sync/snapshots` | Fetch snapshot summaries by datastore |
| `GET` | `/pbs/sync/jobs` | Fetch backup job summaries |
| `GET` | `/pbs/sync/node` | Fetch node status summary |

## PDM (`/pdm`)

PDM routes operate on locally stored `PDMEndpoint` records. As with PBS, the v1
sync routes fetch and summarize remote data without writing to NetBox. Each sync
route accepts optional `netbox_branch_schema_id` for the future branch-aware
write contract.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/pdm/endpoints` | Create a PDM endpoint record |
| `GET` | `/pdm/endpoints` | List configured PDM endpoints |
| `GET` | `/pdm/endpoints/{endpoint_id}` | Read one PDM endpoint |
| `PUT` | `/pdm/endpoints/{endpoint_id}` | Update one PDM endpoint |
| `DELETE` | `/pdm/endpoints/{endpoint_id}` | Delete one PDM endpoint |
| `GET` | `/pdm/status` | Probe reachability and version for enabled endpoints |
| `GET` | `/pdm/sync/full` | Fetch remote, guest, datastore, and resource summaries |
| `GET` | `/pdm/sync/remotes` | Fetch remote summaries |
| `GET` | `/pdm/sync/guests` | Fetch VM and CT summaries from the global resources endpoint |
| `GET` | `/pdm/sync/datastores` | Fetch PBS datastore summaries across PDM remotes |
| `GET` | `/pdm/sync/resources` | Fetch all resource summaries |

## Ceph (`/ceph` and `/ceph/v2`)

The v1 `/ceph` routes read Proxmox-managed Ceph state through resolved Proxmox
sessions. They return the standard `CephSyncResponse` summary envelope. RGW and
RBD syncs also include reflected inventory in `raw` for `netbox-ceph`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/ceph/status` | Probe Ceph reachability and health per Proxmox endpoint |
| `GET` | `/ceph/sync/full` | Fetch all v1 Ceph summary categories |
| `GET` | `/ceph/sync/status` | Fetch cluster status and metadata summaries |
| `GET` | `/ceph/sync/daemons` | Fetch monitor, manager, and metadata-server summaries |
| `GET` | `/ceph/sync/osds` | Fetch OSD summaries |
| `GET` | `/ceph/sync/pools` | Fetch pool summaries |
| `GET` | `/ceph/sync/filesystems` | Fetch filesystem summaries |
| `GET` | `/ceph/sync/crush` | Fetch CRUSH map and rule summaries |
| `GET` | `/ceph/sync/flags` | Fetch cluster flag summaries |
| `GET` | `/ceph/sync/rgw` | Fetch RGW realm, zonegroup, zone, placement target, user, bucket, and RGW-pool inventory |
| `GET` | `/ceph/sync/rbd` | Fetch RBD pool, image, and snapshot inventory |

The v2 `/ceph/v2` routes are the desired-state orchestration surface. They can
validate payloads, build plans, apply approved operations, stream operation
events, reconcile state, and manage external metric/dashboard provider records.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/ceph/v2/capabilities` | Report provider capabilities for UI gating |
| `POST` | `/ceph/v2/validate` | Validate one desired object or a full desired-state bundle |
| `POST` | `/ceph/v2/plans` | Build and remember a plan |
| `GET` | `/ceph/v2/plans/{plan_id}` | Inspect a remembered plan |
| `POST` | `/ceph/v2/plans/{plan_id}/apply` | Apply a remembered plan |
| `POST` | `/ceph/v2/plan` | Compatibility alias for `POST /ceph/v2/plans` |
| `POST` | `/ceph/v2/apply` | Build then apply a plan from one payload |
| `GET` | `/ceph/v2/operations/{operation_id}` | Inspect an operation run |
| `GET` | `/ceph/v2/operations/{operation_id}/events` | Stream operation progress as SSE |
| `POST` | `/ceph/v2/reconcile` | Reconcile desired state against provider state |
| `GET` | `/ceph/v2/metrics` | Return Ceph metrics |
| `GET` | `/ceph/v2/metrics/sources` | List Prometheus metric sources |
| `POST` | `/ceph/v2/metrics/sources` | Create a Prometheus metric source |
| `POST` | `/ceph/v2/metrics/sources/{source_id}/validate` | Validate a Prometheus metric source |
| `GET` | `/ceph/v2/dashboard/endpoints` | List dashboard endpoints |
| `POST` | `/ceph/v2/dashboard/endpoints` | Create a dashboard endpoint |
| `POST` | `/ceph/v2/dashboard/endpoints/{endpoint_id}/validate` | Validate dashboard reachability |
| `GET` | `/ceph/v2/external/clusters` | List external Ceph clusters |
| `POST` | `/ceph/v2/external/clusters` | Create an external Ceph cluster |
| `POST` | `/ceph/v2/external/clusters/{cluster_id}/capabilities` | Probe capabilities for an external cluster |

## Intent (`/intent`)

Intent routes support the NetBox-to-Proxmox branch workflow. Planning is
read-only. Applying diffs, tagging guests, approving deletion requests, and
executing approved deletion requests are gated by the same write boundary used
by VM operational verbs.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/intent/plan` | Validate NetBox branch diffs before merge confirmation |
| `POST` | `/intent/apply` | Apply permitted QEMU, LXC, or firewall diffs |
| `POST` | `/intent/deletion-requests/{deletion_request_id}/approve` | Approve a pending deletion request |
| `POST` | `/intent/deletion-requests/{deletion_request_id}/reject` | Reject a deletion request |
| `POST` | `/intent/deletion-requests/{deletion_request_id}/execute` | Execute an approved deletion request |
| `PUT` | `/intent/tag-pending-deletion` | Add the pending-deletion Proxmox tag without destroying a guest |
| `PUT` | `/intent/untag-pending-deletion` | Remove the pending-deletion Proxmox tag |

## Cloud (`/cloud`)

Cloud routes power the NMS Cloud portal. Most routes that touch Proxmox writes
require `ProxmoxEndpoint.allow_writes=true`; remote SSH execution for the cloud
image build pipeline also requires `PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION=true`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/cloud/templates` | List active NetBox cloud templates |
| `GET` | `/cloud/templates/versions` | List product/version catalog entries for cloud image builds |
| `GET` | `/cloud/vm/templates` | Discover live QEMU Cloud-Init templates from Proxmox |
| `POST` | `/cloud/vm/provision` | Clone/configure a QEMU VM from a template and return JSON |
| `POST` | `/cloud/vm/provision/stream` | Clone/configure a QEMU VM and stream progress as SSE |
| `GET` | `/cloud/lxc/templates` | List LXC templates from Proxmox storage |
| `POST` | `/cloud/lxc/provision` | Create an LXC container |
| `POST` | `/cloud/templates/images` | Build a bootable Proxmox template from a cloud image URL or render the remote pipeline response |
| `POST` | `/cloud/templates/pve` | Render PVE installer cloud-init payloads and optionally create the VM |
| `GET` | `/cloud/proxmox-endpoint/by-url` | Resolve a stored Proxmox endpoint by URL |
| `POST` | `/cloud/image-factory/builds` | Create or dry-run a Packer image factory build |
| `GET` | `/cloud/image-factory/builds/{build_id}` | Inspect an active image factory build |
| `GET` | `/cloud/image-factory/builds/{build_id}/stream` | Stream image factory build progress as SSE |
| `POST` | `/cloud/image-factory/builds/{build_id}/cancel` | Cancel an active image factory build |
| `POST` | `/cloud/image-factory/validate` | Validate an image factory build request without registering a live run |
| `POST` | `/cloud/azure/vhd-imports` | Plan or execute an Azure-exported VHD import into a Proxmox VM shell |
| `POST` | `/cloud/firecracker/provision` | Provision a Firecracker micro-VM through a host-agent |
| `POST` | `/cloud/firecracker/provision/stream` | Provision a Firecracker micro-VM and stream host-agent progress as SSE |

## SSH (`/ssh`)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/ssh/sessions` | Create a short-lived SSH terminal session |
| `WS` | `/ssh/sessions/{session_id}/ws` | Attach to the terminal session over WebSocket |

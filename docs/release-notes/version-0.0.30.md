# proxbox-api 0.0.30

## Summary

NetBox can now add a Proxmox Backup Server (PBS) to a Proxmox cluster as a storage through a
typed, gated and audited operational verb, so a backup of a virtual machine to a PBS can start
from NetBox. Cloud provisioning can now clone a template onto node-local storage of another
node and attach extra network interfaces.

The release adds no backend database migration and no dependency change.

## Release identity and compatibility

| Component | Release or contract |
| --- | --- |
| proxbox-api | `0.0.30` |
| Proxmox SDK | `proxmox-sdk 0.0.15` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 and 3.13 |
| Backend database migration | None |
| Companion plugin | Unchanged from `0.0.29`: a verified `netbox-proxbox` build with the atomic endpoint `sync-cluster` action |

## Typed PBS storage registration

`POST /proxmox/storage/pbs?endpoint_id={id}` adds one Proxmox storage of type `pbs` with
content `backup` and `prune-backups=keep-all=1` to the cluster of the endpoint. The retention is
fixed because Proxmox otherwise applies the storage or node retention after every backup and
can delete older PBS backups, which the PBS prune jobs own. The generated
`POST /proxmox/api2/storage` route stays read-only.

- The `allow_writes` gate of the endpoint runs before body validation.
- Only `endpoint_id` is in the URL. The JSON body carries `storage`, `server`, `port`
  (default `8007`), `datastore`, an optional `namespace`, `username` (an API token id in the
  form `user@realm!token`), the write-only `password`, the required SHA-256 `fingerprint` and
  optional `nodes`.
- The verb is create-only. An equal existing storage answers `already_present` without a write;
  a different one answers HTTP 409 `storage_conflict` with the names of the differing fields; a
  lost create answer causes one configuration read and never a second request.
- An `Idempotency-Key` header replays the answer for 60 seconds, scoped to the endpoint, the verb
  and the storage id.
- Every request that passes the gate writes a NetBox journal entry on the mirrored
  netbox-proxbox `ProxmoxEndpoint`: a write-ahead entry and a terminal update, or one terminal
  entry for an invalid body. A cancelled request still completes its Proxmox call, its journal
  update and its cache entry. A failed terminal update is retried twice; if it still fails, the
  answer is HTTP 502 with `journal_finalized=false`, and a replay with the same key within the
  60-second window repairs the original entry without another Proxmox write.
- The token secret never appears in a response, log, journal comment, exception, validation
  detail or cache entry. An invalid body names only the declared fields that failed, and a
  Proxmox refusal reports its HTTP status and the names of the rejected parameters.

The Proxmox credential of the endpoint needs `Datastore.Allocate` on `/storage`. The
netbox-pbs plugin uses this verb for its "Register as Proxmox storage" action.

## Cross-node cloud clones and extra network interfaces

Proxmox refuses a cross-node clone when the requested storage is not shared or not enabled on
the template node. Cross-node provisioning now reads the cluster storage inventory once:

- A destination storage that is missing or unavailable on the target node is refused with
  `target_storage_unavailable`, and a failed lookup with `storage_lookup_failed`, before any
  clone is created.
- Storage that is shared on the template node keeps the direct clone. Otherwise the VM is
  cloned without a storage override, and a separate `relocate_disk` step moves each eligible
  data disk (not Cloud-Init, CD-ROM, or already on the destination) to the requested storage
  with `move_disk` and `delete=0`, so the source copies remain as unused disks and nothing is
  deleted. JSON responses return the move tasks as `relocate_upids`, and the event stream
  reports the `relocate_disk` step.
- A move failure, a failed or malformed configuration read, or a configuration without disks is
  refused with `relocate_disk_failed`, with the VMID, target node, clone task and the per-disk
  tasks already dispatched; configuration and start do not run.

Provision requests also accept up to eight `extra_networks` entries for `net1` to `net31`, each
with a unique index, a validated bridge, an optional VLAN tag, a model and an optional firewall
flag. An existing MAC address is preserved, an unreadable configuration is refused with
`config_read_failed`, and extra interfaces are refused when the managed cloud network is
enforced.

## Known limitation

Like the other operational verbs, a pending terminal journal update is kept only in the
in-memory idempotency window. After 60 seconds, or after a restart, an entry whose terminal
update failed stays `in_progress` although the Proxmox write completed.

## Release status

`0.0.30rc1` is the release candidate of this version. Package and production availability
follow the release gates.

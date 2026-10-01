# proxbox-api 0.0.24

## Summary

This release makes synchronization safer and more predictable on estates with
several Proxmox endpoints and clusters. It fixes the node-interface flag during
a full update, adds a verified orphan sweep that works for plugin-driven staged
runs, stops one bad VM record from aborting a whole staged run, and prevents VM
matches from crossing cluster boundaries. It also upgrades FastAPI and adds
opt-in native telemetry.

## Fixes and improvements

- **Node interfaces during a full update.** `GET /full-update` and
  `GET /full-update/stream` now forward the resolved behavior flags to the
  node-interface stage, so `sync_node_interfaces=true` takes effect and bridge
  `hwaddress` MAC addresses are written, matching
  `GET /dcim/devices/interfaces/create`.
- **Orphan VM sweep for staged runs.** A standalone, endpoint-scoped sweep is
  available at `GET /virtualization/virtual-machines/orphans/sweep` (plus a
  `/stream` variant). It is fail closed: live runs require an endpoint scope,
  the run must have synchronized VMs in that scope, the VM stage must not have
  failed, and a VM is only marked when its guest is confirmed absent from a
  fresh live Proxmox inventory. Each candidate is re-checked immediately before
  it is changed, and every result reports why it was skipped. Orphans remain
  soft-deleted (status `decommissioning` plus a marker tag); permanent deletion
  stays a human action. Decommissioned VMs are excluded from the disk,
  snapshot, interface and IP stages, a Proxmox "configuration does not exist"
  error is logged as a warning, and a VM that reappears is re-adopted.
- **Staged runs no longer abort on one bad VM.** Staged and estate runs skip a
  VM whose typed ownership record is incomplete, duplicated or unresolvable,
  name it in a warning, process the rest, and report the stage as degraded in
  REST, SSE and full-update results. Requests that address a single VM by id
  stay strict. Skipped VMs are excluded from stale-record cleanup.
- **Cross-cluster matching.** Every match keyed by endpoint and VMID now also
  requires the VM's cluster to match, so interface, IP and snapshot data are no
  longer written to another cluster's VM. A stale stored endpoint id is repaired
  automatically only in the unambiguous case (one candidate of the same type,
  an id that belongs to no configured endpoint, and a matching VM name), which
  prevents ` (2)` duplicate VMs.
- **Platform.** FastAPI is upgraded and opt-in native telemetry is available;
  see the configuration guide.

## Compatibility and migration

This release does not add a database migration and does not change the public
authentication contract. It remains compatible with `proxmox-sdk 0.0.15`,
`netbox-sdk 0.0.13` and the netbox-proxbox `0.0.27` release line. Responses
gain `warnings` and `degraded` fields only when a staged stage skipped a VM.

Known limitations:

- For plugin-driven syncs to sweep orphans, the netbox-proxbox job must call
  the new sweep route after its stages. Until the plugin does, only full-update
  runs sweep.
- Which endpoint id space the plugin sends, and cleanup of legacy records that
  carry stale endpoint ids, are plugin-side concerns. VMs already duplicated as
  `name (2)` are not merged automatically.
- A record with no cluster information can still match across clusters; this is
  tracked for a later release.

## Upgrade

Deploy the exact `proxbox-api 0.0.24` package or published container through
the approved release workflow and restart every backend worker. Verify the
health endpoint, then run one node synchronization and one virtual-machine
synchronization before resuming scheduled full-estate jobs.

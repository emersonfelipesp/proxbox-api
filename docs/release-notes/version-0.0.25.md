# proxbox-api 0.0.25

## Summary

This release hardens synchronization against partial failures. Node-interface
sync no longer erases VLAN assignments when a sub-sync fails, VMs that two
NetBox records claim for the same Proxmox owner are detected, and a VM whose
cluster cannot be verified is no longer written to. It also makes the schema
contract test tolerant of NetBox 4.6 and 4.7 naming and pins the application
server in container images and CI.

## Fixes and improvements

- **Schema contract tolerance.** The schema contract test accepts both the
  NetBox 4.6 and 4.7 names for the virtual machine schema, so the nightly
  schema refresh no longer fails when NetBox renames it.
- **Pinned application server.** granian is pinned to 2.8.4 in the container
  images and in CI, and a guard test fails if the pins drift apart.
- **Node-interface sync keeps VLAN assignments.** With the opt-in
  `sync_node_interfaces` flag, a failed VLAN reconciliation previously caused
  the interface patch to send an empty mode and tagged VLAN list, erasing the
  existing NetBox assignment while the run reported success. Interfaces whose
  VLAN sync failed are now patched without mode and tagged VLANs, so existing
  assignments are preserved. VLAN, IP and MAC failures, including reconciliation
  calls that return no persisted record, are reported as structured warnings and
  mark the result as degraded. This applies to the REST response (which is
  wrapped only when degraded), the SSE completion events and the full update.
  Clean runs return exactly what they returned before.
- **Shared-owner detection.** During sidecar hydration, two NetBox VMs that
  claim the same Proxmox owner (endpoint, cluster, VMID and type) are detected,
  including when only one of them is selected. Strict mode raises before any
  stage writes. Lenient mode skips every claimant, names them in a warning and
  reports the stage as degraded, so no stage writes to or cleans up those
  records.
- **Verified-cluster matching.** A VM whose only endpoint-keyed candidate has no
  verifiable cluster (no cluster data, or an explicit null cluster) is skipped
  with a warning instead of being adopted or duplicated, and the stage is
  reported as degraded. Missing cluster data in the NetBox snapshot is completed
  with bounded concurrency. The Python and Rust engines apply the same rule.

## Compatibility and migration

This release does not add a database migration and does not change the public
authentication contract. It remains compatible with `proxmox-sdk 0.0.15`,
`netbox-sdk 0.0.13` and the netbox-proxbox `0.0.27` release line. Responses gain
`warnings` and `degraded` fields only when a stage is degraded.

## Upgrade

Deploy the exact `proxbox-api 0.0.25` package or published container through
the approved release workflow and restart every backend worker. Verify the
health endpoint, then run one node synchronization and one virtual-machine
synchronization before resuming scheduled full-estate jobs.

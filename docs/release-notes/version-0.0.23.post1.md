# proxbox-api 0.0.23.post1

## Summary

This post-release fixes duplicate virtual-machine discovery records during
Proxmox synchronization. Cluster-wide and node-specific views can report the
same guest in one synchronization run; the backend now deduplicates those
records before concurrent NetBox writes so one guest cannot race to create
multiple virtual machines or retain an incorrect offline status.

## Synchronization reliability

- Deduplicate virtual machines by Proxmox cluster, guest type, and normalized
  positive VMID before scheduling NetBox writes.
- Preserve the first canonical discovery record while rejecting duplicate
  cluster/node representations of the same guest.
- Reject booleans, malformed identifiers, and non-positive VMIDs from the
  deduplication key so invalid values cannot collide with valid guests.
- Keep QEMU and LXC identities separate when their numeric VMIDs match.
- Preserve the synchronized runtime status from the canonical guest instead of
  allowing a duplicate discovery record to leave the NetBox row offline.

## Compatibility

This release has no database migration and does not change the public HTTP,
WebSocket, authentication, or configuration contracts from `0.0.23`. It keeps
the certified `proxmox-sdk 0.0.15` and `netbox-sdk 0.0.13` dependency pairing.
It is a drop-in replacement for `0.0.23` and is the minimum backend version for
the corrected all-in-one `netbox-proxbox` OCI testing appliance.

## Upgrade

Deploy the exact `proxbox-api 0.0.23.post1` package or published container
through the approved release workflow, then restart every backend worker. No
schema migration or data conversion is required. Run one normal synchronization
and verify that each Proxmox guest maps to one NetBox virtual machine with the
expected status before resuming scheduled synchronization.

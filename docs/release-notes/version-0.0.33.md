# proxbox-api 0.0.33

## Summary

This release establishes stable Proxmox VE 9.3 guest UID identity, fixes node-network
synchronization across multiple nodes, and synchronizes runtime DHCP addresses for LXC
containers. It also updates the certified Proxmox SDK to `proxmox-sdk 0.0.16.post1`, which adds
Proxmox VE 9.3 schema support.

The release adds no backend database migration. The paired `netbox-proxbox 0.0.33` release adds
the `proxmox_vm_uid` and `proxmox_cluster_uid` fields in migration `0109`. Deploy this backend
first; older plugin releases continue to work through feature detection.

## Release identity and compatibility

| Component | Release or contract |
| --- | --- |
| proxbox-api | `0.0.33` |
| Proxmox SDK | `proxmox-sdk 0.0.16.post1` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 and 3.13 |
| Backend database migration | None |
| Companion plugin | `netbox-proxbox 0.0.33` |

## Stable Proxmox VE 9.3 guest UID identity

Proxmox VE 9.3 guest and cluster UIDs now provide stable VM identity across VMID changes on the
same endpoint, migration, snapshot rollback, backup, and restore. Exact
`(cluster-id, uid)` matches take precedence over legacy endpoint and VMID matching while retaining
the NetBox cross-cluster guard.

VMID reuse within the same Proxmox cluster creates a new NetBox VM when the stored and live guest
UIDs differ. Missing UIDs and changed cluster UIDs keep the previous matching behavior. Cluster
UIDs persist independently when guest UIDs are temporarily absent, and all UID-filtered responses
are verified client-side. A UID match is scoped to the live endpoint and a verified NetBox
cluster, so a UID never rebinds a VM row owned by another configured endpoint.

The backend sends UID fields only after serializer support is detected from bounded metadata or
non-empty rows; empty rows remain unknown. This feature detection keeps older `netbox-proxbox`
releases compatible. The paired `netbox-proxbox 0.0.33` release persists the identities in its
`proxmox_vm_uid` and `proxmox_cluster_uid` fields introduced by migration `0109`.

## Node-network synchronization

Node-network synchronization no longer stops after the first node. With
`sync_node_interfaces` enabled, each node's interface reconciliation now matches only that node's
own interfaces. Previously, a node whose interface did not yet exist in NetBox could match the
same-named interface, such as `vmbr0` or `eno1`, on another node. NetBox rejected the move with
`Components cannot be moved to a different device.`, and the `network-interfaces` stage failed
for every following node. Contributed by Giovanni Condello.

## LXC runtime DHCP addresses

LXC containers configured with `ip=dhcp` now have their runtime IP addresses synchronized. Full
VM synchronization and the interface-only and IP-only stages read container addresses from the
Proxmox LXC interfaces endpoint because containers have no QEMU guest agent. Stopped containers
are skipped without a request, and a failed read falls back to the static configuration. Running
containers now also receive `netbox-proxbox` guest interface rows under the default
`guest_os_model` strategy. Contributed by Philipp Schwab.

## Upgrade and deployment

Deploy the exact `proxbox-api 0.0.33` package or published container through the approved
release workflow. Deploy this backend first, then deploy the paired `netbox-proxbox 0.0.33`
release and apply its migration `0109`. Backend-first deployment is safe because UID fields are
feature-detected and omitted for older plugins.

The SDK pin changes from `proxmox-sdk 0.0.15` to `proxmox-sdk 0.0.16.post1` for Proxmox VE 9.3
schema support. This repository has no database migration for this release.

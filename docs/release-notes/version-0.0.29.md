# proxbox-api 0.0.29

## Release status

These notes describe the planned `0.0.29` release. The current package preparation uses
`0.0.29rc1`. A candidate does not establish final release availability or production readiness.

## Summary

Cluster synchronization now resolves native NetBox clusters through exact Proxmox endpoint
ownership. The backend requests an atomic ownership claim from the companion plugin before
it writes native cluster metadata. This prevents equal cluster names from selecting another
endpoint's cluster. Cluster and standalone endpoints use the same ownership contract.

The release retains the `0.0.28.post1` default NetBox service initialization fix for stored
Proxmox credential decryption. It adds no backend database migration or dependency change.

## Release identity and compatibility

| Component | Release or contract |
| --- | --- |
| proxbox-api candidate | `0.0.29rc1`, toward final `0.0.29` |
| Proxmox SDK | `proxmox-sdk 0.0.15` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 and 3.13 |
| Backend database migration | None |
| Companion plugin | A verified `netbox-proxbox` build with the atomic endpoint `sync-cluster` action |

Successful cluster synchronization requires
`POST /api/plugins/proxbox/endpoints/proxmox/{id}/sync-cluster/` on the companion plugin.
The action must claim the endpoint, native cluster, tracking row, and typed sidecar in one
database transaction. Its response includes the native cluster, tracking identifier, and
strict `created` flag. An optional strict `changed` flag reports substantive native changes;
an omitted flag means `false` for compatibility.

Verify this capability and the runtime service token permissions on the selected plugin build.
These notes do not assert that companion plugin final `0.0.30` is available. The plugin owns
its separate schema migration and release qualification.

## VM node resolution

VM and container node discovery now queries Proxmox's `cluster/resources` endpoint with
`type=vm` and matches the returned guest type as well as its VMID. This avoids an invalid
`type=qemu` or `type=lxc` query and prevents a container and a VM with the same numeric ID
from being confused. Cross-node template cloning submits the clone on the template's
owning node and retains the requested destination node as its target. The standalone
HTTP mock recognizes the same broad guest filter.

## Native cluster ownership

Full updates, individual cluster and node updates, and VM dependency creation use the exact
plugin endpoint identity. Backend database endpoint IDs and plugin endpoint IDs are separate.
The resolver corroborates connection host and port metadata and refuses ambiguous or
disabled endpoints.

An established endpoint relation remains authoritative after a Proxmox cluster rename and
on retries. The backend reconciles native metadata by the primary key returned by the claim.
It does not adopt or repair a cluster through a global name match. Equal names on different
endpoints do not grant shared ownership. A native name and placement conflict produces an
actionable failure; correct the conflicting inventory or placement before retrying.

A missing action, rejected claim, invalid response, or unresolved endpoint stops native
cluster writes. There is no fallback to name-based native cluster creation. Type and site
prerequisites can already exist when the action fails. An unchanged retry reports `unchanged`.
A new cluster reports `created`; a substantive claim change or later metadata update reports
`updated`. Omitted site and tenant fields preserve existing placement.

## Coordinated upgrade

CAUTION: An older backend can still issue name-based writes. Complete its replacement before
you expose the new plugin ownership relation.

1. Deploy this backend guard before the companion plugin upgrade.
2. Verify the exact backend revision. Replace or drain the deployment-owned backend execution cohort and its in-flight old-code work.
3. Deploy and migrate the qualified companion plugin through its approved release procedure.
4. Verify the atomic action, exact endpoint identity, and runtime service token permissions.
5. Run authenticated staging acceptance for cluster and standalone synchronization before production approval.

While the plugin lacks the action, affected synchronization fails transiently before native
cluster metadata writes. Keep this fail-closed behavior during the upgrade interval. Do not
restore a name-based fallback to suppress the failure.

Validate the endpoint/native-cluster relation, a rename, an unchanged retry, and equal names
on distinct endpoints. Review plugin migration diagnostics for ambiguous legacy inventory.
Use the plugin's documented recovery procedure rather than a name-wide tracking repair.
Authenticated staging acceptance and the release gates remain required before production.

See [Synchronization Workflows](../sync/workflows.md#endpoint-owned-native-clusters) for the
identity rules, response semantics, placement behavior, and deployment sequence.

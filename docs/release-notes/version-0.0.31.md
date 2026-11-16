# proxbox-api 0.0.31

## Summary

This release limits virtual machine disk and snapshot synchronization to the selected Proxmox
endpoint, resolves the NetBox schema contract once for a synchronization run instead of once per
virtual machine, preserves an operator-managed `dns_name` when Proxmox has no hostname, and
reflects the Proxmox start-at-boot setting on native NetBox virtual machines.

The release adds no backend database migration, API contract change, or dependency change.

## Release identity and compatibility

| Component | Release or contract |
| --- | --- |
| proxbox-api | `0.0.31rc1` |
| Proxmox SDK | `proxmox-sdk 0.0.15` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 and 3.13 |
| Backend database migration | None |
| Companion plugin | `netbox-proxbox 0.0.32` |

## Endpoint-scoped disk and snapshot synchronization

A disk or snapshot synchronization without an explicit virtual machine selection still reads the
NetBox virtual machine inventory, but it now starts per-VM work only for virtual machines owned by
the Proxmox endpoints in that run. A virtual machine is excluded when its synchronization state
names an endpoint, every active Proxmox session exposes an endpoint identity, and none matches.

Virtual machines without an endpoint identity remain eligible, and explicit virtual machine
selections remain unchanged. The excluded virtual machines are not counted, do not emit progress
rows, and do not produce skip warnings. The existing ownership checks still protect NetBox writes
and stale-object cleanup.

The disk and snapshot stages also report their total duration and the NetBox request count and
wait time observed during the stage. A NetBox request taking at least five seconds emits a warning
with its HTTP method and path, without query values.

## One NetBox schema contract resolution per run

Virtual machine payload validation now reuses one resolved NetBox OpenAPI contract inside each
proxbox-api process. Concurrent workers share the first resolution instead of downloading and
parsing the complete NetBox schema for every virtual machine.

`PROXBOX_NETBOX_SCHEMA_CONTRACT_TTL` controls reuse and defaults to `3600` seconds. Setting it to
`0` disables reuse. A contract obtained from the on-disk cache or bundled fallback after a live
fetch failure is reused for no more than 60 seconds, so a recovered NetBox is retried promptly.
Changing the configured NetBox endpoint invalidates the in-memory contract, and schema download
and validation work run outside the event loop.

## Operator DNS names and start-at-boot state

IP address reconciliation no longer treats a missing Proxmox hostname as an instruction to clear
an existing NetBox `dns_name`. This applies to individual and bulk synchronization regardless of
the DNS-name overwrite flag. New addresses still receive a NetBox-compatible empty value when no
hostname is available, and an actual resolved hostname remains subject to the configured overwrite
policy.

QEMU and LXC virtual machines now write NetBox's native start-at-boot field from the Proxmox
`onboot` setting. Existing virtual machines converge on their next synchronization. When the guest
configuration is unavailable, synchronization leaves the NetBox field unchanged.

## Upgrade and deployment

Deploy the exact `proxbox-api 0.0.31rc1` package or candidate container through the approved
release workflow and restart every backend worker so each process begins with the new version and
an empty in-memory schema-contract cache. Deploy this backend first, then the paired
`netbox-proxbox 0.0.32` release; the latest releases of the two projects are always paired. Keep `proxmox-sdk 0.0.15` and `netbox-sdk 0.0.13`;
this release does not require dependency upgrades or a backend database migration.

After deployment, run synchronization for each configured endpoint and verify that disk and
snapshot counts contain only that endpoint's virtual machines, operator DNS names remain intact
when Proxmox supplies no hostname, and start-at-boot values match Proxmox. After a NetBox schema
upgrade, restart proxbox-api or wait for the configured schema-contract time to live before
validating the new contract.

## Known limitations

- Endpoint filtering cannot exclude a virtual machine whose synchronization state has no endpoint
  identity, or a run whose Proxmox sessions do not all expose endpoint identities. Existing
  ownership checks continue to prevent cross-endpoint writes.
- The schema contract cache is process-local. Each backend process resolves its own contract, and
  a NetBox schema change is visible after the time to live expires or the process restarts.
- A missing hostname preserves an existing `dns_name`; it does not provide a way to request that
  synchronization clear an operator-managed value.
- Start-at-boot reconciliation requires a readable Proxmox guest configuration. When that read is
  unavailable, NetBox retains its current value.

## Release status

`0.0.31rc1` is the release candidate of this version. Package and production availability follow
the release gates.

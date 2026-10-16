# proxbox-api 0.0.27.post4

## Summary

This post-release hardens Proxmox node device identity resolution. It prevents
stale or unverifiable NetBox synchronization evidence from selecting the wrong
device and makes genuine identity conflicts actionable. It adds no database
migration, API contract change, or dependency change.

## Release identity

| Component | Release or contract |
| --- | --- |
| proxbox-api | `0.0.27.post4` |
| Paired NetBox plugin | `netbox-proxbox 0.0.29.post6` |
| Proxmox SDK | `proxmox-sdk 0.0.15` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 and 3.13 |
| Database migration | None |

## Proxmox node device identity resolution

Node synchronization now verifies every candidate identity returned by NetBox
before adopting it. A synchronization-state row must match the requested
cluster and node, have compatible endpoint ownership, and reference the exact
device returned by a device-id lookup. Rows that reference deleted devices or
a different identity are ignored.

An exact ownership claim for the selected Proxmox endpoint outranks an
otherwise matching unclaimed legacy row. If NetBox ignores an identifier
filter, returns a malformed response, or otherwise prevents the backend from
verifying the requested record, resolution fails closed instead of treating
the lookup as proof that the device was deleted. This keeps conflicting
evidence visible and prevents synchronization from modifying the wrong device.

When more than one live device remains a valid candidate, the ambiguity error
lists the conflicting NetBox device ids and synchronization-state row ids. It
also directs the operator to confirm the correct device before removing stale
device or synchronization-state records.

## Compatibility

The HTTP, SSE, WebSocket, database, and dependency contracts are unchanged.

| NetBox plugin version | Compatibility |
| --- | --- |
| `netbox-proxbox 0.0.29.post6` | Paired and supported release. |
| `netbox-proxbox 0.0.29.post5` | Compatible with the unchanged backend API; upgrade to the paired release for the certified stack. |
| Earlier releases | No API contract was removed, but use only combinations covered by their published compatibility guidance. |

## Upgrade notes

1. Upgrade proxbox-api to `0.0.27.post4` and the paired NetBox plugin to
   `netbox-proxbox 0.0.29.post6`.
2. Restart every backend worker after installing the package or container.
3. No database migration or schema conversion is required.
4. Run a Proxmox node synchronization and confirm that each node resolves to
   the expected NetBox device.

## Known limitations and operator actions

- The backend deliberately does not choose between multiple live devices with
  equally valid identity evidence. Confirm the authoritative device, then
  remove the obsolete device or synchronization-state rows named by the error.
- A NetBox response that cannot prove it honored the requested identifier
  remains a hard resolution failure. Correct the NetBox or plugin lookup
  behavior before retrying synchronization.
- Stale synchronization-state rows are ignored during resolution but are not
  deleted automatically.

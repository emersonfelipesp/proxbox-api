# proxbox-api 0.0.30.post1

## Summary

This post-release fixes the NetBox journal writer of the operational verbs. In 0.0.30 every verb
that records a write-ahead journal entry refused before it reached Proxmox, although NetBox had
created the entry: the typed `POST /proxmox/storage/pbs` verb answered HTTP 502, and the start,
stop, reboot, snapshot, migrate, delete, backup and snapshot-delete verbs answered HTTP 409. It adds no
database migration, API contract change, or dependency change.

## Release identity

| Component | Release or contract |
| --- | --- |
| proxbox-api | `0.0.30.post1` |
| Proxmox SDK | `proxmox-sdk 0.0.15` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 and 3.13 |
| Database migration | None |

## Write-ahead journal entry

The verbs fail closed when they cannot record their write-ahead journal entry, and they need its
identifier to write the terminal update. The NetBox REST layer answers a create with a record
object whose fields are reached through its serialization, but the journal writer accepted only
a plain dictionary or an object with a `data` attribute. It therefore returned nothing for every
entry it created, and each verb refused with "The NetBox audit journal entry returned no ID" or
"write-ahead journal create returned no id". The created entry stayed at `result: in_progress`.

The journal writer now returns the fields of that record, so the verbs keep the identifier of
their entry, reach Proxmox, and finalize the same entry. Regression tests use the record type
that NetBox returns instead of a plain dictionary.

## Upgrade

Deploy the exact `proxbox-api 0.0.30.post1` package or published container through the approved
release workflow and restart every backend worker. Journal entries that 0.0.30 left at
`result: in_progress` remain as an audit of the refused requests; no Proxmox change was made for
them. Retry the refused operation after the upgrade.

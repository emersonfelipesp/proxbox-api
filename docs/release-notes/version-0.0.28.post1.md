# proxbox-api 0.0.28.post1

## Summary

This post-release fixes a failure to decrypt stored Proxmox endpoint credentials after the
0.0.28 deployment. Cloud template discovery and the signed template-build preflight answered
HTTP 503 with "Could not decrypt Proxmox endpoint credentials" in every worker that had not yet
handled a request which uses the default NetBox service. It adds no database migration, API
contract change, or dependency change.

## Release identity

| Component | Release or contract |
| --- | --- |
| proxbox-api | `0.0.28.post1` |
| Proxmox SDK | `proxmox-sdk 0.0.15` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 and 3.13 |
| Database migration | None |

## Plugin encryption key acquisition

When no operator key is configured, the backend obtains its encryption key from the NetBox
plugin. The plugin key authority requires that the default NetBox service has been selected in
the worker before it will request the key. Routes that open a Proxmox session from a stored
endpoint, such as `GET /cloud/vm/templates` and `POST /cloud/templates/images/preflight`, do not
select that service, so a worker that had served no NetBox-backed request found no key and
reported the stored credentials as undecryptable.

Initial key acquisition now runs the same default-service selection that every NetBox session
request uses: the enabled NetBox endpoint with the lowest identifier. It runs once, only while
nothing is designated, and designates nothing when no enabled endpoint exists. It never replaces
an existing designation, never reselects after a retirement or a changed connection input, does
not run for a check against an expected binding, and is not re-entered from inside the selection.
A selection failure reads as "no default yet" and the request fails closed as before.

## Upgrade

Deploy the exact `proxbox-api 0.0.28.post1` package or published container through the approved
release workflow and restart every backend worker. Verify one cloud template discovery request
and one image-build preflight; neither needs a prior sync to succeed. No operator key or
configuration change is required.

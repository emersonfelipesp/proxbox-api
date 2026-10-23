# proxbox-api 0.0.30.post2

## Summary

This post-release restores cluster synchronization against netbox-proxbox releases that do not
have the `sync-cluster` action, such as 0.0.29.post3. Since 0.0.29, every synchronization that
resolves a native NetBox cluster called that action and stopped when NetBox answered with its
"Page Not Found" page. Full updates, individual cluster, node, VM, device, and storage
synchronization, and VM creation were affected. It adds no database migration, API contract
change, or dependency change.

## Release identity

| Component | Release or contract |
| --- | --- |
| proxbox-api | `0.0.30.post2` |
| Proxmox SDK | `proxmox-sdk 0.0.15` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 and 3.13 |
| Database migration | None |

## Plugins without the claim action

Before it claims a cluster, the backend now sends one GET to the exact action URL. The request
uses no cache, follows no redirect, and reads a bounded body. Only an HTTP 404 with an HTML body
proves that the plugin has no such route. Every other answer, including a JSON 404, 403, 405, a
server error, or a timeout, keeps the atomic endpoint-owned claim unchanged. A plugin that has
the action therefore never uses the new path.

When the route is absent, the backend adopts an existing native cluster read-only, and only
through the plugin's own endpoint-scoped tracking rows. Every read in this path bypasses all
caches. The backend's NetBox token must belong to a superuser, proven by its own user matching
`is_superuser=true` and not `is_superuser=false`, so that the tracking rows it reads are the
complete set. The endpoint is resolved again from a complete page and must be the same enabled
endpoint with the same transport identity, all tracking rows come from one complete page, and the
cluster is read by ID and its identity is validated. It adopts native cluster C for the
endpoint only when all of these are true:

- Exactly one tracking row of the endpoint has the exact cluster name, and that row links C.
- Every linked tracking row of the endpoint links C.
- No tracking row of another endpoint links C.
- The current name of C equals the cluster name.

These conditions are the same uniqueness rule that the netbox-proxbox 0.0.31 migration uses to
bind endpoints to their clusters, so the backend adopts only pairs that the plugin upgrade binds
itself. The backend reads every tracking row once and makes all comparisons itself, so a server
that ignores a list filter cannot widen the match. It never creates, links, renames, or matches
a cluster by name across endpoints, and it never writes the adopted cluster or its sync-state
sidecar; the cluster is only referenced by the endpoint's devices and VMs. With any token other than a
superuser's, adoption is refused. In every other case the synchronization stops with a message
that names the missing action and the plugin upgrade.

## Upgrade

Deploy the exact `proxbox-api 0.0.30.post2` package or published container through the approved
release workflow and restart every backend worker. Clusters that are not yet linked to their
endpoint by a tracking row still need netbox-proxbox 0.0.31 or later. This compatibility path
will be removed when every supported environment runs a plugin with the action.

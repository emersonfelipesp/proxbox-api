# Version 0.0.21.post2

proxbox-api `0.0.21.post2` pairs with `netbox-proxbox 0.0.26.post1`,
`proxmox-sdk 0.0.13`, and `netbox-sdk 0.0.13`. The package supports Python
3.12 and 3.13. The post release is required because `v0.0.21` belongs to an
unrelated historical commit and the `v0.0.21.post1` publication failed before
checkout on a trusted runner without Node.js. Both tags remain immutable.

## Console relay authentication

- Adds the bounded `websocket_auth` contract to console-session responses so
  the trusted NMS relay can authenticate its server-side WebSocket handshake
  to Proxmox.
- Uses `Authorization: PVEAPIToken=...` for API-token endpoints and a
  `PVEAuthCookie=...` cookie for password-backed endpoints.
- Keeps all Proxmox credentials server-side. The browser receives only the
  NMS one-use relay URL and never receives the upstream authentication value.
- Hides sensitive authentication values from dataclass representations and
  rejects malformed response shapes at the NMS boundary.

## Inventory and synchronization

- Removes the legacy NetBox custom-field inventory path. Reflection state now
  uses the typed `Proxbox*SyncState` sidecars supplied by netbox-proxbox.
- Removes the deprecated custom-field reconciliation routes and their runtime
  settings. Existing NetBox custom-field definitions and stored values are not
  deleted by the upgrade.
- Preserves Proxmox VM notes, bootstraps required NetBox support objects before
  dependent sync stages, and retains the application-factory wiring required by
  the production service.

## Release and deployment integrity

- Checks out the exact public tag without a JavaScript action, allowing the
  minimal trusted publication runner to operate without Node.js.
- Builds the release sdist with the immutable offline Docker context required
  by the production deployment controller.
- Installs dependencies from hash-pinned wheels with no package index or
  network access, then installs the project without dependency resolution.
- Inventories the hash-pinned requirements file together with every wheel and
  verifies the extracted sdist before publication.

## Upgrade

Deploy the exact `proxbox-api 0.0.21.post2` package from the Gitea Package
Registry, verify `/health`, and validate QEMU noVNC, QEMU terminal, and LXC
terminal sessions through the NMS same-origin relay.

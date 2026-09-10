# Version 0.0.21.post5

proxbox-api `0.0.21.post5` pairs with `netbox-proxbox 0.0.26.post1`,
`proxmox-sdk 0.0.13`, and `netbox-sdk 0.0.13`. It contains the authenticated
QEMU and LXC console-session contract introduced in `0.0.21.post2`.

## Package-first deployment provenance

- Selects exactly one version-qualified wheel and one version-qualified source
  distribution from the build output.
- Copies only those two immutable artifacts into a new private staging
  directory before generating the canonical release manifest.
- Uploads only the selected wheel and source distribution instead of expanding
  an ambient distribution-directory glob.
- Binds the protected tag SHA, file names, byte lengths, and SHA-256 digests
  consumed by the management backend's signed production deployment-proof
  contract.
- Rejects missing, duplicated, mismatched, or additional staged artifacts
  before any registry mutation.

## Upgrade

Deploy the exact `proxbox-api 0.0.21.post5` package through the management
backend's `latest_package` source. Verify `/health`, then validate QEMU
noVNC, QEMU terminal, and LXC terminal sessions through the management
backend's same-origin relay.

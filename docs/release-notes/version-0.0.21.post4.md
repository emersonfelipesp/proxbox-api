# Version 0.0.21.post4

proxbox-api `0.0.21.post4` pairs with `netbox-proxbox 0.0.26.post1`,
`proxmox-sdk 0.0.13`, and `netbox-sdk 0.0.13`. It contains the authenticated
QEMU and LXC console-session contract introduced in `0.0.21.post2`.

## Package-first deployment provenance

- Creates and freezes the canonical release manifest before the registry client
  can modify the distribution directory.
- Uploads the exact wheel and offline source distribution described by that
  manifest.
- Publishes the frozen manifest as an immutable generic package linked to the
  canonical `emersonfelipesp/proxbox-api` repository.
- Binds the protected tag SHA, file names, byte lengths, and SHA-256 digests
  consumed by the NMS signed production deployment-proof contract.

## Upgrade

Deploy the exact `proxbox-api 0.0.21.post4` package through the NMS
`latest_package` source. Verify `/health`, then validate QEMU noVNC, QEMU
terminal, and LXC terminal sessions through the NMS same-origin relay.

# Version 0.0.21.post3

proxbox-api `0.0.21.post3` pairs with `netbox-proxbox 0.0.26.post1`,
`proxmox-sdk 0.0.13`, and `netbox-sdk 0.0.13`. It contains the authenticated
QEMU and LXC console-session contract introduced in `0.0.21.post2`.

## Package-first deployment provenance

- Publishes a canonical `release-manifest.json` after the wheel and offline
  source distribution have been uploaded to the Gitea Package Registry.
- Binds the manifest to the exact protected tag commit and the SHA-256 digest
  and byte length of both distribution files.
- Publishes the manifest as an immutable generic package and links it to the
  canonical `emersonfelipesp/proxbox-api` repository.
- Allows the NMS signed deployment-proof contract to resolve, verify, and
  authorize the exact package artifacts before production mutation.

## Upgrade

Deploy the exact `proxbox-api 0.0.21.post3` package through the NMS
`latest_package` source. Verify `/health`, then validate QEMU noVNC, QEMU
terminal, and LXC terminal sessions through the NMS same-origin relay.

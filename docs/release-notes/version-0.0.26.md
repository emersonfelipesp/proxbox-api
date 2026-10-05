# proxbox-api 0.0.26

## Summary

This release fixes node synchronization failing with "Ambiguous Proxmox node
device identity" and aligns plugin settings and credential handling with the
NetBox sensitive-data contract.

## Fixes and improvements

- **Node device identity no longer blocks every sync.** When several identity
  sidecars pointed at different NetBox devices for one cluster and node, for
  example after a device was renamed, moved or deleted, every stage failed with
  HTTP 503. Sidecars for deleted devices are now ignored. Device names are not
  treated as evidence, because operators may rename node devices. The target
  cluster decides ownership, and a site match counts only for devices that have
  no cluster. If more than one live device is still equally valid, the stage
  still fails closed so a foreign device is never adopted.
- **Sensitive-data contract for plugin settings and credentials.** Runtime key
  disclosure requires the plugin's active superuser or an explicit per-user
  sensitive-data grant. Metadata compatibility is used only after an HTTP 404,
  never after authorization, transport or other runtime failures, and it never
  retains the encryption key. See the configuration guide for details.

## Compatibility and migration

This release does not add a database migration and does not change the public
authentication contract. Deployments that relied on the previous settings and
credential fallback behavior should review the configuration guide.

## Upgrade

Deploy the exact `proxbox-api 0.0.26` package or published container through
the approved release workflow and restart every backend worker. Verify the
health endpoint, then run one node synchronization and one virtual-machine
synchronization before resuming scheduled full-estate jobs.

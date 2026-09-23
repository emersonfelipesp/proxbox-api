# proxbox-api 0.0.23.post2

## Summary

This post-release restores complete virtual-machine backup synchronization when
the Proxmox SDK returns a typed VM identity object. It also restores the audited
Gitea package-publication path needed to keep private and public release
channels aligned.

## Backup synchronization

- Hydrate the typed virtual-machine identity before calling the backup
  synchronization helper.
- Preserve the normalized VMID and guest type throughout the backup workflow
  instead of passing an incomplete raw record.
- Cover QEMU and LXC backup discovery, mixed input representations, missing
  identities, and downstream synchronization failures.

## Release integrity

- Build release artifacts on an untrusted runner without publication
  credentials, then transfer only the exact wheel and source distribution to
  the credentialed publication job.
- Download and SHA-256 verify the pinned `uv 0.11.28` bootstrap, invoking it by
  absolute path so ambient runner tooling cannot shadow the approved binary.
- Validate immutable package identity and artifact digests before publication,
  link the package to its source repository, and publish a repository-linked
  canonical release manifest.
- Read the canonical package authority from trusted repository configuration
  and reject missing or malformed non-HTTPS values before authenticated
  requests.

## Test reliability

- Preserve application-level WebSocket assertions when teardown cancellation
  races with disconnect handling.
- Keep teardown cleanup cancellation-safe without hiding the original test
  oracle.

## Compatibility

This release has no database migration and does not change the public HTTP,
WebSocket, authentication, or configuration contracts from `0.0.23.post1`. It
keeps the certified `proxmox-sdk 0.0.15` and `netbox-sdk 0.0.13` dependency
pairing. It is a drop-in replacement for `0.0.23.post1` and is the backend
version intended for the all-in-one `netbox-proxbox` OCI testing appliance.

## Upgrade

Deploy the exact `proxbox-api 0.0.23.post2` package or published container
through the approved release workflow, then restart every backend worker. No
schema migration or data conversion is required. Run a backup synchronization
for one QEMU or LXC guest and verify that its expected backup records appear in
NetBox before resuming scheduled synchronization.

# proxbox-api 0.0.28

## Summary

This release adds extra data disks, Proxmox tags, and descriptions to QEMU VM
provisioning. It also adds caller-supplied Cloud-Init vendor-data to the Cloud
Image Build Pipeline.

## Fixes and improvements

- **Provision extra QEMU data disks.** The QEMU provision routes accept up to
  eight extra SCSI, VirtIO, or SATA disks. Each disk can specify its slot,
  storage, size, discard setting, and SSD setting where supported. The backend
  reads the cloned VM configuration before it writes any extra disk. It sends the configuration
  digest with the write so Proxmox can reject a concurrent configuration
  change. It reports slot conflicts as HTTP 409 and invalid storage as HTTP 422.
  When storage is omitted, the backend infers it only from an attached disk.
- **Set Proxmox tags and descriptions during provisioning.** QEMU provision
  requests can set Proxmox tags and a description on the cloned VM. Requests
  that omit these fields keep the previous configuration payload.
- **Supply a first-boot vendor script.** The Cloud Image Build Pipeline accepts
  `first_boot_script` as an alternative to `user_data_yaml`. It stores the
  script as a Cloud-Init vendor-data snippet. The signed plan digest binds the
  exact script content. A stable SHA-256 content prefix in the snippet name
  prevents separate signed builds from overwriting each other's scripts. The
  vendor-data path leaves the per-VM Cloud-Init fields available on clones.

## Compatibility and migration

This release does not add a database migration and does not change the public
authentication contract. The new provisioning fields are additive. A build
request cannot specify both `first_boot_script` and `user_data_yaml`.

## Upgrade

Deploy the exact `proxbox-api 0.0.28` package or published container through
the approved release workflow and restart every backend worker. Verify the
health endpoint. Then run one QEMU provision request and one image-build plan
before resuming scheduled provisioning jobs.

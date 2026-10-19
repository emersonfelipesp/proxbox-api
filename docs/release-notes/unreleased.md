# Unreleased

- Add extra data disks, Proxmox tags, and descriptions to QEMU VM provisioning. Extra-disk requests now require a readable cloned-VM configuration, use its digest for the configuration write, and infer default storage only from attached disks.
- Accept a caller-supplied `first_boot_script` (cloud-init vendor-data) in the Cloud Image Build Pipeline, bound by the signed plan digest, so a baked template keeps the per-VM Cloud-Init fields. Caller vendor snippet names now include a stable SHA-256 content prefix to prevent independently signed builds from overwriting each other's scripts.

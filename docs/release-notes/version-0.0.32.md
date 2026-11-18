# proxbox-api 0.0.32

## Summary

This release adds Talos Linux as a direct-only Cloud Image Build Pipeline product. A Talos nocloud
qcow2 is downloaded through the Proxmox API, imported without modification, attached to a new
virtual machine with the standard cloud-init drive, and converted to a template.

The release adds one accepted value, `talos`, to the existing `product_type` enum in the
`POST /cloud/templates/images` request schema. It adds no new route, backend database migration,
dependency change, or response-schema change.

## Release identity and compatibility

| Component | Release or contract |
| --- | --- |
| proxbox-api | `0.0.32rc1` |
| Proxmox SDK | `proxmox-sdk 0.0.15` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 and 3.13 |
| Backend database migration | None |
| Companion plugin | `netbox-proxbox 0.0.32.post2` (paired release, published with this version) |

## Unmodified Talos image import

Talos has no shell or package manager, so the backend does not send it through a script-based
image customization path. The direct build downloads the requested image into Proxmox import
storage, imports it as the template's boot disk, adds the standard cloud-init drive, and converts
the virtual machine to a template. proxbox-api generates no product user-data, writes no snippet,
and does not modify the downloaded image.

Preserving the source image bytes keeps Talos and Omni SideroLink kernel arguments baked into an
Image Factory schematic intact. Proxmox can still write its default cloud-config to the separate
cloud-init drive; Talos ignores that user-data and reads only the network configuration from the
drive.

## Request contract

Call `POST /cloud/templates/images` with `product_type="talos"`, an explicit `image_url` for the
Talos nocloud qcow2, and the existing direct-build fields including `endpoint_id` and
`target_node`. The general-purpose default image URL is never accepted for Talos.

Talos requests are direct-only. Pipeline selectors and customization inputs such as `execute`,
`provider`, `user_data_yaml`, and `first_boot_script` are rejected. Existing product types and
their request behavior are unchanged.

## Upgrade and deployment

Deploy the exact `proxbox-api 0.0.32rc1` package or candidate container through the approved
release workflow. Deploy this backend with the paired `netbox-proxbox 0.0.32.post2` release;
the latest releases of the two projects are always paired. Keep `proxmox-sdk 0.0.15` and
`netbox-sdk 0.0.13`; this release does not require dependency upgrades or a backend database
migration.

Before creating a Talos template, use a Talos Image Factory nocloud qcow2 URL whose schematic
contains the required machine configuration and any Omni SideroLink kernel arguments. Existing
endpoint write and template-build authorization gates still apply to the direct Proxmox mutation.

## Known limitations

- Talos builds require an explicit image URL and cannot use the request's default image.
- Talos builds cannot use the SSH pipeline, custom cloud-init user-data, or first-boot scripts.
- proxbox-api does not inspect or alter the Talos schematic embedded in the image; callers remain
  responsible for selecting the correct Image Factory artifact.

## Release status

`0.0.32rc1` is the release candidate of this version. Package and production availability follow
the release gates.

# proxbox_api/routes/cloud Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/routes/cloud/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Cloud runtime routes mounted at `/cloud/*`: live QEMU Cloud-Init template
discovery, QEMU/Firecracker provisioning, the image catalog/factory, PVE
template listing, the **Cloud Image Build Pipeline** that bakes bootable
Proxmox VM templates from a cloud image plus a cloud-init `#cloud-config`, and
the **Azure VHD Import Pipeline** that downloads an Azure-exported VHD,
converts it to QCOW2, and attaches it to a generated Proxmox VM shell.

## Modules

- `templates.py` — QEMU template listing/catalog helpers.
- `qemu_templates.py` — `GET /cloud/vm/templates`, live Proxmox QEMU template
  discovery for an endpoint. It filters cluster resources to QEMU templates and,
  by default, only returns templates whose config contains a Cloud-Init drive or
  `cicustom` metadata.
- `catalog.py` — `/cloud/catalog` tenant-visible catalog.
- `image_factory.py` — `/cloud/template-images` image factory.
- `pve_templates`/`pve_template.py` — PVE template listing + the operator-facing
  `ssh root@host` + `tee` block helper.
- `cloud_init_templates.py`, `pve_cloudinit_payload.py` — cloud-init payload
  helpers.
- `template_images.py` — `POST /cloud/templates/images` (the build entrypoint).
- `pipeline_scripts.py` — builds the remote bake script and runs it over SSH.
- `azure_vhd_imports.py` — `POST /cloud/azure/vhd-imports`, the Azure disk
  import entrypoint.
- `azure_vhd_pipeline.py` — renders the `curl` + `qemu-img convert` + `qm`
  import script and optionally runs it over SSH.
- `provision.py`, `provision_stream.py` — QEMU provision (REST + SSE).
- `firecracker.py` — `/cloud/firecracker/provision` (+ stream).

## Cloud Image Build Pipeline (`POST /cloud/templates/images`)

`build_cloud_image_template()` in `template_images.py` has two paths:

1. **Direct Proxmox API build** — when `execute`, `provider`, `user_data_yaml`
   are all unset and the product is a plain image: requires `endpoint_id`,
   `target_node`, `image_url`; downloads via the Proxmox storage API and builds
   through the proxmox-sdk session.
2. **Pipeline build** — when `execute is not None`, `provider is not None`,
   `user_data_yaml is not None`, or the product is pfSense/OPNsense: routes to
   `build_pipeline_response(req)` in `pipeline_scripts.py`, which renders a bash
   bake script (download image, `qm` create, write the `cicustom` user-data
   snippet, `qm template`) and, when `execute=true`, runs it on the Proxmox host
   over SSH.

### `cicustom` cloud-init snippet (why this exists)

A Proxmox `cicustom` user-data snippet is the **only** mechanism that runs a full
`#cloud-config` at first boot — the native cloud-init drive and the REST upload
API cannot. When `user_data_yaml` is set, the pipeline writes it verbatim to
`<vm_storage>:snippets/<name>-pve-custom-user-data.yml` and sets
`cicustom=user=...` on the template. The schema field is on
`CloudImageTemplateBuildRequest` in `schemas/cloud_provision.py`
(`user_data_yaml: str | None`, max 65536, `extra="forbid"`).

### Remote SSH execution (gating + identity)

- Gated by `PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION=true` (a 403 with the
  enable-instruction is returned otherwise).
- The ssh command is `ssh -p <port> [-i <ssh_identity_file>] <user>@<ssh_host>
  'bash -s'`. `ssh_identity_file` must resolve under `PROXBOX_SSH_KEY_DIR`
  (validated in `schemas/cloud_provision.py`); `ssh_host` is validated against
  option-injection.
- The runtime image bakes in `openssh-client` (Dockerfile `runtime-base`, since
  `0.0.18.post1`) — never rely on an in-container `apk add`.
- When the request sends no `-i`, the host provides the bake key as the default
  `/root/.ssh/id_ed25519` (see the compose mounts + host bootstrap doc below).

### Who calls it

`netbox-packer`'s `PackerBuildJob` (cloud_config installer) calls this endpoint
via `proxbox_client.call_proxbox_build()` with `X-Proxbox-API-Key`, passing
`user_data_yaml = installer_config.content`. The whole flow is triggerable from
the NMS UI at `nms.nmulti.cloud/virtualization/packer`. See
`/root/personal-context/claude-reference/netbox-packer.md`.

Host bootstrap (bake key, storage content types `snippets,import,images`,
`allow_writes=True`, NetBox Packer settings):
`/root/personal-context/nmulticloud-context/deploy/docs/proxbox-api-cloud-image-bake.md`.
Compose wiring: `nmulticloud-context/deploy/compose/proxbox-api.compose.yaml`.

## Azure VHD Import Pipeline (`POST /cloud/azure/vhd-imports`)

`create_azure_vhd_import()` in `azure_vhd_imports.py` validates an
`AzureVhdImportRequest`, calls `_gate()` when `execute=true`, and delegates to
`build_azure_vhd_import_response()` in `azure_vhd_pipeline.py`.

The response always returns the generated operator script, which:

1. preflights required host tools, destination node name, VMID availability,
   target storage, and bridge presence,
2. downloads the Azure-exported VHD to `/var/lib/vz/template/cache/` with
   `curl -C -` so interrupted downloads can resume,
3. validates source and converted images with `qemu-img info`,
4. converts the VHD with `qemu-img convert -f vpc -O qcow2`,
5. creates the Proxmox VM shell with Gen1/Gen2-aware BIOS defaults,
6. imports the QCOW2 with `qm importdisk`, parses the returned volid from
   command output, and
7. attaches the imported volume as either `scsi0` (Linux) or `sata0`
   (Windows-safe first boot).

Execution details:

- `execute=true` is gated by `PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION=true`.
- `endpoint_id` is required for execute mode so `_gate()` can enforce
  `ProxmoxEndpoint.allow_writes`.
- `ssh_host`, `ssh_user`, and optional `ssh_identity_file` reuse the same SSH
  validation boundary as the cloud-image build pipeline.
- The Windows-safe profile intentionally uses `sata0` + `e1000` for first boot;
  Linux defaults to `virtio-scsi-single` + `scsi0` with `discard=on` and
  `iothread=1`.

## Extension Guidance

- Keep request validation/normalization in `schemas/cloud_provision.py`, not in
  the route or pipeline helper modules.
- Preserve the `cicustom` snippet path — it is the contract that makes a full
  `#cloud-config` actually execute on cloned VMs.
- Keep SSH identity restricted to `PROXBOX_SSH_KEY_DIR`; never accept an
  arbitrary `-i` path or interpolate `ssh_host` without the existing validators.

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
- `network.py` — `GET /cloud/network/available-ips`, a read-only NetBox
  available-IP peek for the configured customer prefix.
- `firecracker.py` — `/cloud/firecracker/provision` (+ stream).
- `lxc.py` — `GET /cloud/lxc/templates` (read-only CT-template listing) and
  `POST /cloud/lxc/provision` (write). **Gate distinction:** template listing is a
  read and resolves the endpoint via `_endpoint_for_read` (existence + `enabled`),
  the same read gate as `qemu_templates.py` — it must NOT use the `allow_writes`
  write gate `_gate` (doing so 403'd the Templates tab on write-disabled
  endpoints). `provision_lxc` is a real write and keeps `_gate`.

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
   over SSH. This path sets `qm ... --agent enabled=1` before templating so
   clones inherit the Proxmox-side QEMU guest agent setting.

QEMU VM provisioning (`POST /cloud/vm/provision` and
`POST /cloud/vm/provision/stream`) accepts optional `sockets`, `bridge`,
`vlan_tag`, and `disk_gb` overrides plus `enable_agent` (default `True`). They
are applied through the Proxmox API after clone and before start, preserving the
existing `net0` model/MAC when overriding bridge or VLAN tag, and forcing
`agent=enabled=1` on the clone when `enable_agent` is true. The nested
`cloud_init` payload accepts an optional `password` that is written as the
Proxmox `cipassword` cloud-init field, so a cloned VM supports username+password
SSH when the source template also permits password auth (`ssh_pwauth: true`,
baked by netbox-packer). `cipassword`/`password` are treated as secrets: they are
never logged and are redacted by `utils/log_scrubbing.scrub_cloud_init` at the
journal/write boundary. The redaction is applied **on every provisioning error
surface** (#222): the QEMU REST step-rollback wrapper scrubs even on the default
`enforce_cloud_network=False` path (parity with the SSE stream, which always
scrubs), and the LXC `provision_lxc` failure handler scrubs the 502 body + log
line too (the LXC create carries a `password` field). `CloudInitPayload.password`
is bounded to `max_length=128`.

QEMU and LXC provisioning also accept `enforce_cloud_network` (default
`false`). When true, proxbox-api resolves the customer-network settings from
`ProxboxPluginSettings` through `runtime_settings`, requires a configured
`cloud_customer_prefix_id`, `cloud_customer_bridge`, and
`cloud_customer_gateway`, allocates the next available NetBox IP from
`POST /api/ipam/prefixes/{id}/available-ips/`, and ignores caller-supplied
bridge/VLAN/IP values in favor of the configured bridge, VLAN tag, allocated
CIDR, and gateway. QEMU injects the allocated CIDR through Cloud-Init
`ipconfig0` and applies bridge/VLAN through `net0`; LXC sends
`net0=name=eth0,bridge=...,tag=...,ip=...,gw=...` during create.

If Proxmox provisioning fails after a NetBox allocation, the route calls the
cloud-network service's best-effort `release_ip()` rollback. After successful
QEMU provisioning, proxbox-api attempts to bind the allocated IP to the VM's
first NetBox VMInterface when the VM/interface rows already exist; if sync has
not produced them yet, the address remains occupied in NetBox and the skip is
logged rather than released.

`GET /cloud/network/available-ips?limit=N` returns the configured prefix id,
gateway, bridge, VLAN tag, lock flag, and a non-occupying list of available
addresses from NetBox. It returns HTTP 409 with `cloud network not configured`
when the required customer-network settings are missing.

For Proxmox Backup Server images, prefer the pipeline path by setting
`provider="debian_cloud_image"` so the generated PBS `#cloud-config` is written
as a `cicustom` user-data snippet. The catalog default is PBS `4.2` on Debian
Trixie (`debian-13-genericcloud-amd64.qcow2`), with product defaults that
install `proxmox-backup-server`, `qemu-guest-agent`, and `zabbix-agent2`.
Operators can override DNS search domain, nameservers, QGA, Zabbix Agent 2, and
Zabbix server through `CloudImageTemplateBuildRequest`.

For Proxmox VE images, the mounted catalog must use
`provider="proxmox_iso"` and official Proxmox VE installer ISO media. Reject
`provider="debian_cloud_image"` for PVE products; do not return to the older
pattern of installing `proxmox-ve` on a Debian generic cloud image. Generated
PVE installer/template setup must use a graphical VGA display (`std` unless
there is a product-specific reason to change it) so the Proxmox UI opens a
usable noVNC console. Keep `serial0` + `vga serial0` only for serial appliance
images that intentionally require it, currently pfSense and OPNsense serial
release/source builds.

### `cicustom` cloud-init snippet (why this exists)

A Proxmox `cicustom` user-data snippet is the **only** mechanism that runs a full
`#cloud-config` at first boot — the native cloud-init drive and the REST upload
API cannot. When `user_data_yaml` is set, the pipeline writes it verbatim to
`<vm_storage>:snippets/<name>-pve-custom-user-data.yml` and sets
`cicustom=user=...` on the template. The schema field is on
`CloudImageTemplateBuildRequest` in `schemas/cloud_provision.py`
(`user_data_yaml: str | None`, max 65536, `extra="forbid"`).

For Proxmox product snippets, install `curl`/`gnupg`/`ca-certificates` from the
base Debian repositories, fetch `proxmox-release-<codename>.gpg`, and only then
write the Proxmox no-subscription repo. Do not create the Proxmox repo in
`write_files` before `package_update`; cloud-init's first `apt-get update` will
reject the unsigned repo before the key exists and abort the bootstrap.
Remove both legacy `.list` and deb822 `.sources` enterprise repo files before
each apt update, and preseed `grub-pc/install_devices` before installing
Proxmox packages so cloud-init never blocks on an interactive grub prompt.

### Remote SSH execution (gating + identity)

- Gated by `PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION=true` (a 403 with the
  enable-instruction is returned otherwise).
- `endpoint_id` is required when `execute=true`; requests without it fail closed
  with 422 before the pipeline can render or run a script.
- `execute=true` runs both endpoint gates before SSH: `_gate()` enforces
  `ProxmoxEndpoint.allow_writes=True`, then `gate_ssh_access()` enforces
  `ProxmoxEndpoint.access_methods="api_ssh"`. A write-disabled endpoint returns
  403 before any SSH attempt; an API-only endpoint returns 403
  `reason="ssh_not_enabled_for_endpoint"`.
- The ssh command is `ssh -p <port> [-i <ssh_identity_file>] <user>@<ssh_host>
  'bash -s'`. `ssh_identity_file` must resolve under `PROXBOX_SSH_KEY_DIR`
  (validated in `schemas/cloud_provision.py`); `ssh_host` is validated against
  option-injection.
- The runtime image bakes in `openssh-client` (Dockerfile `runtime-base`, since
  `0.0.18.post1`) — never rely on an in-container `apk add`.
- When the request sends no `-i`, the host provides the bake key as the default
  `/root/.ssh/id_ed25519` (see the compose mounts + host bootstrap doc below).

## Firecracker Provisioning Trust Boundary

`firecracker.py` accepts `host_agent_base_url` and optional `host_agent_token`
from the caller because `nms-backend` resolves the selected NetBox Proxbox
Firecracker host/image inventory before calling this backend. proxbox-api still
validates the outbound target before constructing `FirecrackerHostAgentClient`:
the base URL must use `http` or `https`, include a hostname, omit embedded
credentials, omit query strings/fragments, and pass the shared SSRF host guard
(`ssrf.py::validate_endpoint_url`). The bearer token is forwarded only to the
validated host-agent.

Firecracker provisioning is not a `ProxmoxEndpoint` write and does not use
`allow_writes`; the trust boundary is the shared API key, the nms-backend
inventory resolution step, and the host-agent URL SSRF validation above.
Streaming failures are sanitized with the same
`PROXBOX_EXPOSE_INTERNAL_ERRORS` gate as the app-level generic exception
handler: clients see `An unexpected error occurred.` by default.

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

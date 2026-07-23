"""Script-rendered Cloud Image Build Pipeline helpers.

The existing ``template_images`` route can create generic cloud-image templates
through the Proxmox API. These helpers cover the operator-driven path used by
NMS and netbox-proxbox when the source is a pfSense/OPNsense release image or
an appliance source tree that must be built on a Proxmox-capable host.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import shlex
import tempfile
from contextlib import suppress
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from fastapi import HTTPException

from proxbox_api.credentials import derive_service_signing_key
from proxbox_api.logger import logger
from proxbox_api.routes.cloud.catalog import find_product_version
from proxbox_api.routes.cloud.display import display_config_for_product
from proxbox_api.schemas.cloud_image_security import OpenSSHIdentityFile, open_ssh_identity_file
from proxbox_api.schemas.cloud_provision import (
    CloudImageBuildProvider,
    CloudImageBuildTarget,
    CloudImageSourceBuildCommand,
    CloudImageSSHExecutionTarget,
    CloudImageTemplateBuildRequest,
    CloudImageTemplateBuildResponse,
    CloudImageTemplateExecutionSummary,
    CloudImageTemplateSensitivePreview,
    CloudImageVersionEntry,
    PackerFinding,
    PackerFindingSeverity,
)
from proxbox_api.utils.cancellation import await_task_through_repeated_cancellation

_SSH_BINARY = "/usr/bin/ssh"
_SSH_KEYSCAN_BINARY = "/usr/bin/ssh-keyscan"
_LEGACY_PIPELINE_VM_STORAGE = "local-lvm"
_RECIPE_BINDING_CONTEXT = "packer-recipe-binding-v1"


def _q(value: object) -> str:
    return shlex.quote(str(value))


def _encoded_write_command(target: str, content: str, *, target_is_expression: bool = False) -> str:
    """Render a delimiter-proof file write using a base64 data argument."""

    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    destination = target if target_is_expression else _q(target)
    return f"printf '%s' {_q(encoded)} | /usr/bin/base64 -d > {destination}"


def _render_source_build_command(command: CloudImageSourceBuildCommand) -> str:
    """Render one allowlisted command from fixed argv."""

    return shlex.join(command.argv)


def _artifact_from_url(url: str) -> str:
    name = PurePosixPath(urlsplit(url).path).name
    candidate = name or "cloud-image-artifact"
    if not all(character.isalnum() or character in "._-" for character in candidate):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "image_artifact_name_invalid",
                "message": "The catalog image URL does not have a safe artifact filename.",
            },
        )
    return candidate


def _catalog_image_url(
    request: CloudImageTemplateBuildRequest,
    entry: CloudImageVersionEntry,
) -> str | None:
    if "image_url" in request.model_fields_set and request.image_url:
        return request.image_url
    return entry.image_url


def _decompressed_name(filename: str, compression: str | None) -> str:
    if compression and filename.endswith(f".{compression}"):
        return filename[: -(len(compression) + 1)]
    for suffix in (".gz", ".bz2", ".xz"):
        if filename.endswith(suffix):
            return filename[: -len(suffix)]
    return filename


def _decompress_command(path: str, compression: str | None) -> str | None:
    if compression == "gz" or path.endswith(".gz"):
        return f"gunzip -kf {_q(path)}"
    if compression == "bz2" or path.endswith(".bz2"):
        return f"bunzip2 -kf {_q(path)}"
    if compression == "xz" or path.endswith(".xz"):
        return f"xz -dkf {_q(path)}"
    return None


def _debian_major(codename: str) -> str | None:
    return {
        "bullseye": "11",
        "bookworm": "12",
        "trixie": "13",
    }.get(codename)


def _service_names(
    entry: CloudImageVersionEntry,
    *,
    package_name: str,
    install_qemu_guest_agent: bool,
    install_zabbix_agent2: bool,
) -> list[str]:
    services = [entry.service_name or package_name]
    if entry.product_type.value == "pbs":
        services.append("proxmox-backup")
    if install_qemu_guest_agent:
        services.append("qemu-guest-agent")
    if install_zabbix_agent2:
        services.append("zabbix-agent2")
    deduped: list[str] = []
    for service in services:
        if service and service not in deduped:
            deduped.append(service)
    return deduped


def _agent_flags(
    entry: CloudImageVersionEntry,
    request: CloudImageTemplateBuildRequest,
) -> tuple[bool, bool]:
    qga = request.install_qemu_guest_agent
    zabbix = request.install_zabbix_agent2
    return (
        entry.product_type.value == "pbs" if qga is None else qga,
        entry.product_type.value == "pbs" if zabbix is None else zabbix,
    )


def _install_packages(
    *,
    package_name: str,
    install_qemu_guest_agent: bool,
    install_zabbix_agent2: bool,
) -> list[str]:
    packages = [package_name]
    if install_qemu_guest_agent:
        packages.append("qemu-guest-agent")
    if install_zabbix_agent2:
        packages.append("zabbix-agent2")
    return packages


def _append_dns_config(
    lines: list[str],
    request: CloudImageTemplateBuildRequest,
) -> None:
    if not request.search_domain and not request.nameservers:
        return
    lines.extend(["manage_resolv_conf: true", "resolv_conf:"])
    if request.nameservers:
        lines.append("  nameservers:")
        lines.extend(f"    - {server}" for server in request.nameservers)
    if request.search_domain:
        lines.extend(["  searchdomains:", f"    - {request.search_domain}"])


def _preferences_lines(
    entry: CloudImageVersionEntry,
    request: CloudImageTemplateBuildRequest,
    package_name: str,
) -> list[str]:
    if not request.pve_version_pin:
        return []
    return [
        f"  - path: /etc/apt/preferences.d/nmulticloud-{entry.product_type.value}-pin",
        "    content: |",
        f"      Package: {package_name}",
        f"      Pin: version {request.pve_version_pin}*",
        "      Pin-Priority: 1001",
    ]


def _append_zabbix_config(lines: list[str], zabbix_server: str) -> None:
    lines.extend(
        [
            "  - |",
            "    set -eu",
            "    install -d /etc/zabbix",
            "    touch /etc/zabbix/zabbix_agent2.conf",
            "    if grep -q '^Server=' /etc/zabbix/zabbix_agent2.conf; then",
            f"      sed -i 's|^Server=.*|Server={zabbix_server}|' /etc/zabbix/zabbix_agent2.conf",
            "    else",
            f"      printf 'Server={zabbix_server}\\n' >> /etc/zabbix/zabbix_agent2.conf",
            "    fi",
            "    if grep -q '^ServerActive=' /etc/zabbix/zabbix_agent2.conf; then",
            (
                f"      sed -i 's|^ServerActive=.*|ServerActive={zabbix_server}|' "
                "/etc/zabbix/zabbix_agent2.conf"
            ),
            "    else",
            (f"      printf 'ServerActive={zabbix_server}\\n' >> /etc/zabbix/zabbix_agent2.conf"),
            "    fi",
        ]
    )


def generate_cloud_init_userdata(
    entry: CloudImageVersionEntry,
    request: CloudImageTemplateBuildRequest,
) -> str:
    codename = entry.debian_codename or request.debian_release
    repo_suite = entry.repo_suite or entry.product_type.value
    repo_component = entry.repo_component or f"{repo_suite}-no-subscription"
    package_name = entry.package_name or "proxmox-ve"
    install_qemu_guest_agent, install_zabbix_agent2 = _agent_flags(entry, request)
    install_packages = _install_packages(
        package_name=package_name,
        install_qemu_guest_agent=install_qemu_guest_agent,
        install_zabbix_agent2=install_zabbix_agent2,
    )
    services = _service_names(
        entry,
        package_name=package_name,
        install_qemu_guest_agent=install_qemu_guest_agent,
        install_zabbix_agent2=install_zabbix_agent2,
    )
    ssh_keys = "\n".join(f"    - {key}" for key in request.ssh_authorized_keys)
    ssh_block = f"\nssh_authorized_keys:\n{ssh_keys}" if ssh_keys else ""
    zabbix_release_url = None
    if install_zabbix_agent2 and (debian_major := _debian_major(codename)):
        zabbix_release_url = (
            "https://repo.zabbix.com/zabbix/7.4/release/debian/pool/main/z/"
            f"zabbix-release/zabbix-release_latest_7.4+debian{debian_major}_all.deb"
        )
    lines = [
        "#cloud-config",
        f"hostname: {request.hostname}",
        f"fqdn: {request.hostname}.{request.domain}",
    ]
    _append_dns_config(lines, request)
    lines.extend(
        [
            "package_update: true",
            "package_upgrade: true",
            "write_files:",
            f"  - path: /etc/apt/sources.list.d/{entry.product_type.value}-install-repo.list",
            "    content: |",
            (
                "      deb [arch=amd64] "
                f"http://download.proxmox.com/debian/{repo_suite} {codename} {repo_component}"
            ),
        ]
    )
    lines.extend(_preferences_lines(entry, request, package_name))
    lines.extend(
        [
            "runcmd:",
            (
                "  - curl -fsSL -o "
                f"/etc/apt/trusted.gpg.d/proxmox-release-{codename}.gpg "
                f"https://enterprise.proxmox.com/debian/proxmox-release-{codename}.gpg"
            ),
            "  - rm -f /etc/apt/sources.list.d/pve-enterprise.list /etc/apt/sources.list.d/pbs-enterprise.list",
        ]
    )
    if zabbix_release_url:
        lines.extend(
            [
                f"  - curl -fsSL -o /tmp/zabbix-release.deb {zabbix_release_url}",
                "  - dpkg -i /tmp/zabbix-release.deb",
            ]
        )
    lines.extend(
        [
            "  - DEBIAN_FRONTEND=noninteractive apt-get update",
            f"  - DEBIAN_FRONTEND=noninteractive apt-get install -y {' '.join(install_packages)}",
        ]
    )
    if install_zabbix_agent2:
        _append_zabbix_config(lines, request.zabbix_server)
    lines.extend(f"  - systemctl enable {service}" for service in services)
    lines.extend(["power_state:", "  mode: poweroff", "  condition: true"])
    if ssh_block:
        lines.extend(ssh_block.strip("\n").splitlines())
    return "\n".join(lines) + "\n"


def generate_appliance_first_boot_script(
    entry: CloudImageVersionEntry,
    request: CloudImageTemplateBuildRequest,
) -> str:
    nameservers = " ".join(request.nameservers)
    ssh_keys = "\n".join(request.ssh_authorized_keys)
    environment = (
        "\n".join(
            [
                'PIPELINE_NAME="Cloud Image Build Pipeline"',
                f'PRODUCT="{entry.product_type.value}"',
                f'PRODUCT_VERSION="{entry.version}"',
                f'HOSTNAME="{request.hostname}"',
                f'DOMAIN="{request.domain}"',
                f'NODE_CIDR="{request.node_cidr or ""}"',
                f'GATEWAY="{request.gateway or ""}"',
                f'NAMESERVERS="{nameservers}"',
            ]
        )
        + "\n"
    )
    authorized_keys = f"{ssh_keys}\n" if ssh_keys else ""
    commands = [
        "#!/bin/sh",
        "set -eu",
        'MARKER="/var/db/nmulticloud-cloud-image-bootstrap.done"',
        '[ -f "$MARKER" ] && exit 0',
        "mkdir -p /usr/local/etc/nmulticloud /root/.ssh /var/db",
        "chmod 700 /root/.ssh",
        _encoded_write_command("/usr/local/etc/nmulticloud/cloud-image.env", environment),
        _encoded_write_command("/root/.ssh/authorized_keys", authorized_keys),
        "chmod 600 /root/.ssh/authorized_keys",
        f"hostname {_q(request.hostname)}",
        f"printf '%s\\n' {_q(request.hostname)} > /etc/myname 2>/dev/null || true",
        'touch "$MARKER"',
    ]
    return "\n".join(commands) + "\n"


def _append_template_import_commands(
    commands: list[str],
    request: CloudImageTemplateBuildRequest,
    entry: CloudImageVersionEntry,
    qcow_path: str,
    first_boot_script: str | None,
    user_data: str | None,
    *,
    qcow_path_is_expression: bool = False,
) -> None:
    snippet_name = f"{request.name}-{entry.product_type.value}-{entry.version}".replace("/", "-")
    storage = request.vm_storage
    commands.extend(
        [
            (
                f"qm create {_q(request.vmid)} --name {_q(request.name)} "
                f"--memory {_q(request.memory_mb)} --cores {_q(request.cores)} "
                f"--net0 {_q('virtio,bridge=' + request.bridge)}"
            ),
            (
                f"qm importdisk {_q(request.vmid)} "
                f"{qcow_path if qcow_path_is_expression else _q(qcow_path)} {_q(storage)}"
            ),
            (
                f"IMPORTED_VOLID=$(pvesm list {_q(storage)} --vmid {_q(request.vmid)} "
                "| awk 'NR == 2 {print $1}')"
            ),
            'test -n "${IMPORTED_VOLID}"',
            f"qm set {_q(request.vmid)} --scsihw virtio-scsi-pci --scsi0 " + '"${IMPORTED_VOLID}"',
            f"qm set {_q(request.vmid)} --ide2 {_q(request.vm_storage + ':cloudinit')}",
            f"qm set {_q(request.vmid)} --boot order=scsi0",
            f"qm set {_q(request.vmid)} --agent enabled=1",
        ]
    )
    display = display_config_for_product(entry.product_type)
    if display.serial0:
        commands.append(
            f"qm set {_q(request.vmid)} --serial0 {_q(display.serial0)} --vga {_q(display.vga)}"
        )
    else:
        commands.append(f"qm set {_q(request.vmid)} --vga {_q(display.vga)}")
    if request.disk_size_gb:
        commands.append(f"qm resize {_q(request.vmid)} scsi0 {_q(str(request.disk_size_gb) + 'G')}")

    snippet_refs: list[str] = []
    if user_data:
        user_volid = f"{request.snippets_storage}:snippets/{snippet_name}-user-data.yml"
        commands.append(f"USER_SNIPPET_PATH=$(pvesm path {_q(user_volid)})")
        commands.append(
            _encoded_write_command('"$USER_SNIPPET_PATH"', user_data, target_is_expression=True)
        )
        snippet_refs.append(f"user={user_volid}")
    if first_boot_script:
        boot_volid = f"{request.snippets_storage}:snippets/{snippet_name}-first-boot.sh"
        commands.append(f"BOOT_SNIPPET_PATH=$(pvesm path {_q(boot_volid)})")
        commands.append(
            _encoded_write_command(
                '"$BOOT_SNIPPET_PATH"', first_boot_script, target_is_expression=True
            )
        )
        commands.append('chmod 600 "$BOOT_SNIPPET_PATH"')
        snippet_refs.append(f"vendor={boot_volid}")
    meta_volid = f"{request.snippets_storage}:snippets/{snippet_name}-meta-data.yml"
    meta_data = f"instance-id: {snippet_name}\nlocal-hostname: {request.hostname}\n"
    commands.append(f"META_SNIPPET_PATH=$(pvesm path {_q(meta_volid)})")
    commands.append(
        _encoded_write_command('"$META_SNIPPET_PATH"', meta_data, target_is_expression=True)
    )
    snippet_refs.append(f"meta={meta_volid}")
    commands.append(f"qm set {_q(request.vmid)} --cicustom {_q(','.join(snippet_refs))}")
    commands.append(
        f"qm set {_q(request.vmid)} --description {_q('Built by Cloud Image Build Pipeline')}"
    )
    commands.append(f"qm template {_q(request.vmid)}")


def _release_image_script(
    request: CloudImageTemplateBuildRequest,
    entry: CloudImageVersionEntry,
    first_boot_script: str | None,
    user_data: str | None,
) -> tuple[str, list[str], str]:
    image_url = _catalog_image_url(request, entry)
    if not image_url:
        raise HTTPException(
            status_code=422, detail="image_url is required for release image builds."
        )
    filename = _artifact_from_url(image_url)
    raw_filename = _decompressed_name(filename, entry.compression)
    commands = [
        "set -euo pipefail",
        "umask 077",
        f"STAGING_DIR=$(mktemp -d {_q(f'/var/tmp/proxbox-cloud-image-{request.vmid}.XXXXXX')})",
        "trap 'rm -rf -- \"$STAGING_DIR\"' EXIT",
        f'CACHE_PATH="$STAGING_DIR/{filename}"',
        f'RAW_PATH="$STAGING_DIR/{raw_filename}"',
        'QCOW_PATH="$STAGING_DIR/template.qcow2"',
        f"if qm status {_q(request.vmid)} >/dev/null 2>&1; then exit 73; fi",
        f'curl -fL --retry 3 -o "$CACHE_PATH" {_q(image_url)}',
    ]
    sha256 = request.sha256 or entry.sha256
    if sha256:
        commands.append(f"printf '%s  %s\\n' {_q(sha256)} \"$CACHE_PATH\" | sha256sum -c -")
    if entry.compression == "gz" or filename.endswith(".gz"):
        commands.append('gunzip -kf "$CACHE_PATH"')
    elif entry.compression == "bz2" or filename.endswith(".bz2"):
        commands.append('bunzip2 -kf "$CACHE_PATH"')
    elif entry.compression == "xz" or filename.endswith(".xz"):
        commands.append('xz -dkf "$CACHE_PATH"')
    commands.append('qemu-img convert -O qcow2 "$RAW_PATH" "$QCOW_PATH"')
    _append_template_import_commands(
        commands,
        request,
        entry,
        '"$QCOW_PATH"',
        first_boot_script,
        user_data,
        qcow_path_is_expression=True,
    )
    return "\n".join(commands) + "\n", commands, image_url


def _proxmox_iso_script(
    request: CloudImageTemplateBuildRequest,
    entry: CloudImageVersionEntry,
) -> tuple[str, list[str], str]:
    image_url = _catalog_image_url(request, entry)
    if not image_url:
        raise HTTPException(status_code=422, detail="image_url is required for Proxmox ISO builds.")
    if not image_url.lower().split("?", 1)[0].endswith(".iso"):
        raise HTTPException(
            status_code=422,
            detail="provider=proxmox_iso requires a Proxmox installer .iso image_url.",
        )

    filename = _artifact_from_url(image_url)
    iso_volid = f"{request.image_storage}:iso/{filename}"
    storage = request.vm_storage
    display = display_config_for_product(entry.product_type)
    commands = [
        "set -euo pipefail",
        "umask 077",
        f"STAGING_DIR=$(mktemp -d {_q(f'/var/tmp/proxbox-cloud-image-{request.vmid}.XXXXXX')})",
        "trap 'rm -rf -- \"$STAGING_DIR\"' EXIT",
        'DOWNLOADED_ISO="$STAGING_DIR/installer.iso"',
        f"if qm status {_q(request.vmid)} >/dev/null 2>&1; then exit 73; fi",
        f"ISO_PATH=$(pvesm path {_q(iso_volid)})",
        'install -d "$(dirname -- "$ISO_PATH")"',
        f'curl -fL --retry 3 -o "$DOWNLOADED_ISO" {_q(image_url)}',
        'install -m 600 "$DOWNLOADED_ISO" "$ISO_PATH"',
        (
            f"qm create {_q(request.vmid)} --name {_q(request.name)} "
            f"--memory {_q(request.memory_mb)} --cores {_q(request.cores)} "
            f"--net0 {_q('virtio,bridge=' + request.bridge)}"
        ),
        f"qm set {_q(request.vmid)} --ostype {_q(request.os_type)}",
        f"qm set {_q(request.vmid)} --scsihw virtio-scsi-pci --scsi0 {_q(storage + ':0')}",
        f"qm set {_q(request.vmid)} --ide2 {_q(iso_volid + ',media=cdrom')}",
        f"qm set {_q(request.vmid)} --boot {_q('order=ide2;scsi0')}",
        f"qm set {_q(request.vmid)} --agent enabled=1",
    ]
    if request.cpu:
        commands.append(f"qm set {_q(request.vmid)} --cpu {_q(request.cpu)}")
    if display.serial0:
        commands.append(
            f"qm set {_q(request.vmid)} --serial0 {_q(display.serial0)} --vga {_q(display.vga)}"
        )
    else:
        commands.append(f"qm set {_q(request.vmid)} --vga {_q(display.vga)}")
    if request.disk_size_gb:
        commands.append(f"qm resize {_q(request.vmid)} scsi0 {_q(str(request.disk_size_gb) + 'G')}")
    commands.append(
        f"qm set {_q(request.vmid)} --description "
        f"{_q('Proxmox VE installer VM built by Cloud Image Build Pipeline')}"
    )
    return "\n".join(commands) + "\n", commands, image_url


def _source_tree_script(
    request: CloudImageTemplateBuildRequest,
    entry: CloudImageVersionEntry,
    first_boot_script: str | None,
) -> tuple[str, list[str]]:
    build_command = entry.source_build_command
    if build_command is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "source_recipe_unavailable",
                "message": "No server-owned source recipe exists for this catalog entry.",
            },
        )
    source_tree = build_command.source_root
    artifact_path = f"{source_tree}/{build_command.artifact_relative_path}"
    if request.source_build_command is not None and request.source_build_command != build_command:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "source_recipe_assertion_mismatch",
                "message": "The caller recipe assertion does not match the server catalog.",
            },
        )
    if request.source_tree_path is not None and request.source_tree_path != source_tree:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "source_root_assertion_mismatch",
                "message": "The caller source-root assertion does not match the server recipe.",
            },
        )
    if request.source_artifact_path is not None and request.source_artifact_path != artifact_path:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "source_artifact_assertion_mismatch",
                "message": "The caller artifact assertion does not match the server recipe.",
            },
        )
    commands = [
        "set -euo pipefail",
        "umask 077",
        f"EXPECTED_SOURCE_ROOT={_q(source_tree)}",
        'SOURCE_ROOT=$(realpath -e -- "$EXPECTED_SOURCE_ROOT")',
        'test "$SOURCE_ROOT" = "$EXPECTED_SOURCE_ROOT"',
        'test "$(stat -c %U -- "$SOURCE_ROOT")" = root',
        'SOURCE_MODE=$(stat -c %a -- "$SOURCE_ROOT")',
        'test "$((0$SOURCE_MODE & 0022))" -eq 0',
        f"RECIPE_COMMAND={_q(build_command.argv[0])}",
        'test -x "$RECIPE_COMMAND"',
        'test "$(stat -c %U -- "$RECIPE_COMMAND")" = root',
        'RECIPE_MODE=$(stat -c %a -- "$RECIPE_COMMAND")',
        'test "$((0$RECIPE_MODE & 0022))" -eq 0',
        f"if qm status {_q(request.vmid)} >/dev/null 2>&1; then exit 73; fi",
        'cd -- "$SOURCE_ROOT"',
        _render_source_build_command(build_command),
        (f'ARTIFACT_PATH=$(realpath -e -- "$SOURCE_ROOT/{build_command.artifact_relative_path}")'),
        'case "$ARTIFACT_PATH" in "$SOURCE_ROOT"/*) ;; *) exit 74 ;; esac',
        'test -f "$ARTIFACT_PATH"',
        'test "$(stat -c %U -- "$ARTIFACT_PATH")" = root',
        'ARTIFACT_MODE=$(stat -c %a -- "$ARTIFACT_PATH")',
        'test "$((0$ARTIFACT_MODE & 0022))" -eq 0',
        f"STAGING_DIR=$(mktemp -d {_q(f'/var/tmp/proxbox-cloud-image-{request.vmid}.XXXXXX')})",
        "trap 'rm -rf -- \"$STAGING_DIR\"' EXIT",
        'QCOW_PATH="$STAGING_DIR/template.qcow2"',
        'qemu-img convert -O qcow2 "$ARTIFACT_PATH" "$QCOW_PATH"',
    ]
    _append_template_import_commands(
        commands,
        request,
        entry,
        '"$QCOW_PATH"',
        first_boot_script,
        None,
        qcow_path_is_expression=True,
    )
    return "\n".join(commands) + "\n", commands


def _custom_userdata_entry(
    request: CloudImageTemplateBuildRequest,
) -> CloudImageVersionEntry:
    """Build a synthetic catalog entry for a verbatim ``user_data_yaml`` build.

    Used when the caller supplies cloud-init user-data directly (for example a
    netbox-packer ``cloud_config`` installer config) instead of selecting a
    Proxmox product from the catalog. The entry only needs enough fields for the
    release-image/source-tree script builders and the snippet label.
    """
    provider = request.provider or CloudImageBuildProvider.release_image
    return CloudImageVersionEntry(
        version=request.product_version or "custom",
        label=request.name,
        product_type=request.product_type,
        default_provider=provider,
        supported_providers=[provider],
        os_family="linux",
        image_url=request.image_url,
    )


def _catalog_pipeline_inputs(
    request: CloudImageTemplateBuildRequest,
) -> tuple[CloudImageVersionEntry, CloudImageBuildProvider, str | None, str | None]:
    """Resolve (entry, provider, user_data, first_boot_script) from the product catalog."""
    try:
        entry = find_product_version(request.product_type, request.product_version)
    except KeyError:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "catalog_entry_not_found",
                "message": "No catalog entry matches the requested product and version.",
            },
        ) from None

    provider = request.provider or entry.default_provider
    if entry.product_type.value == "pve" and provider == CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE:
        raise HTTPException(
            status_code=422,
            detail=(
                "PVE products must use provider=proxmox_iso; "
                "debian_cloud_image builds are not supported for Proxmox VE."
            ),
        )
    if provider not in entry.supported_providers:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "provider_not_supported",
                "message": "The selected provider is not supported by the catalog entry.",
            },
        )

    user_data: str | None = None
    first_boot_script: str | None = None
    if entry.product_type.value == "firecracker":
        from proxbox_api.routes.cloud.cloud_init_templates import generate_firecracker_userdata

        user_data = generate_firecracker_userdata(
            os_family=entry.os_family,
            os_codename=entry.os_codename or entry.debian_codename or request.debian_release,
        )
    elif entry.product_type.value in {"pbs", "pdm"}:
        user_data = generate_cloud_init_userdata(entry, request)
    else:
        first_boot_script = generate_appliance_first_boot_script(entry, request)
    return entry, provider, user_data, first_boot_script


def _resolve_pipeline_inputs(
    request: CloudImageTemplateBuildRequest,
) -> tuple[CloudImageVersionEntry, CloudImageBuildProvider, str | None, str | None]:
    """Pick the build inputs from either a verbatim user_data_yaml or the catalog."""
    if request.user_data_yaml is not None:
        # Verbatim cloud-init user-data path: skip the product catalog entirely and
        # bake the supplied #cloud-config as the cicustom user snippet over SSH.
        entry = _custom_userdata_entry(request)
        provider = request.provider or CloudImageBuildProvider.release_image
        return entry, provider, request.user_data_yaml, None
    return _catalog_pipeline_inputs(request)


class _SSHHostKeyVerificationError(RuntimeError):
    """Internal fixed-message boundary for a failed pinned-host-key check."""


class _SSHIdentityFileVerificationError(RuntimeError):
    """Internal fixed-message boundary for an untrusted SSH private key."""


class PipelineExecutionCancelled(asyncio.CancelledError):
    """Cancellation carrying only bounded, secret-free execution metadata."""

    def __init__(self, execution: CloudImageTemplateExecutionSummary) -> None:
        super().__init__("Cloud Image Pipeline execution cancelled")
        self.execution = execution


def _fingerprint_from_known_host_line(line: str) -> str | None:
    fields = line.split()
    if len(fields) < 3 or fields[0].startswith("#"):
        return None
    try:
        key_bytes = base64.b64decode(fields[2], validate=True)
    except (ValueError, TypeError):
        return None
    digest = base64.b64encode(hashlib.sha256(key_bytes).digest()).decode().rstrip("=")
    return f"SHA256:{digest}"


async def _read_bounded_stream(
    stream: asyncio.StreamReader,
    *,
    limit: int,
) -> bytes:
    """Read a small control-plane stream and reject it once the bound is exceeded."""

    chunks: list[bytes] = []
    size = 0
    while chunk := await stream.read(4096):
        size += len(chunk)
        if size > limit:
            raise _SSHHostKeyVerificationError
        chunks.append(chunk)
    return b"".join(chunks)


async def _pinned_known_hosts_file(target: CloudImageSSHExecutionTarget) -> Path:
    """Scan once, verify the persisted fingerprint, and pin that exact key."""

    scan: asyncio.subprocess.Process | None = None
    try:
        scan = await asyncio.create_subprocess_exec(
            _SSH_KEYSCAN_BINARY,
            "-T",
            "10",
            "-p",
            str(target.port),
            target.host,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if scan.stdout is None:
            raise _SSHHostKeyVerificationError
        stdout = await asyncio.wait_for(_read_bounded_stream(scan.stdout, limit=65536), 15)
        returncode = await asyncio.wait_for(scan.wait(), 15)
    except asyncio.CancelledError:
        if scan is not None:
            stop_task = asyncio.create_task(_stop_process(scan))
            await await_task_through_repeated_cancellation(stop_task)
        raise
    except (OSError, TimeoutError, _SSHHostKeyVerificationError) as error:
        if scan is not None:
            await _stop_process(scan)
        raise _SSHHostKeyVerificationError from error
    if returncode != 0:
        raise _SSHHostKeyVerificationError
    matching_line = next(
        (
            line
            for line in stdout.decode("utf-8", errors="replace").splitlines()
            if _fingerprint_from_known_host_line(line) == target.known_host_fingerprint
        ),
        None,
    )
    if matching_line is None:
        raise _SSHHostKeyVerificationError

    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="proxbox-packer-known-hosts-",
        delete=False,
    )
    try:
        handle.write(f"{matching_line}\n")
    finally:
        handle.close()
    return Path(handle.name)


def _remove_known_hosts_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning(
            "Cloud Image Pipeline known-hosts cleanup failed error_type=%s",
            type(error).__name__,
        )


def _ssh_argv(
    target: CloudImageSSHExecutionTarget,
    known_hosts_file: Path,
    identity: OpenSSHIdentityFile,
    *remote_argv: str,
) -> list[str]:
    """Build fixed SSH transport options with no ambient config or proxy authority."""

    return [
        _SSH_BINARY,
        "-F",
        "none",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_file}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "ProxyCommand=none",
        "-o",
        "ProxyJump=none",
        "-o",
        "CanonicalizeHostname=no",
        "-p",
        str(target.port),
        "-i",
        identity.child_path,
        f"{target.user}@{target.host}",
        *remote_argv,
    ]


def _open_ssh_identity(target: CloudImageSSHExecutionTarget) -> OpenSSHIdentityFile:
    try:
        return open_ssh_identity_file(target.identity_file)
    except ValueError as error:
        raise _SSHIdentityFileVerificationError from error


async def _stream_stats(stream: asyncio.StreamReader) -> tuple[int, int]:
    """Drain arbitrary output without retaining its secret-bearing contents."""

    byte_count = 0
    line_count = 0
    final_byte = b""
    while chunk := await stream.read(65536):
        byte_count += len(chunk)
        line_count += chunk.count(b"\n")
        final_byte = chunk[-1:]
    if byte_count and final_byte != b"\n":
        line_count += 1
    return byte_count, line_count


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 10)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()


async def _cancel_remote_unit(
    target: CloudImageSSHExecutionTarget,
    known_hosts_file: Path,
    identity: OpenSSHIdentityFile,
    remote_unit: str,
) -> bool:
    """Stop the fixed systemd unit used to isolate a remote build."""

    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            *_ssh_argv(
                target,
                known_hosts_file,
                identity,
                "/usr/bin/systemctl",
                "stop",
                remote_unit,
            ),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            pass_fds=(identity.fd,),
        )
        return await asyncio.wait_for(process.wait(), 30) == 0
    except asyncio.CancelledError:
        if process is not None:
            stop_task = asyncio.create_task(_stop_process(process))
            await await_task_through_repeated_cancellation(stop_task)
        raise
    except TimeoutError:
        if process is not None:
            await _stop_process(process)
        return False
    except (OSError, _SSHIdentityFileVerificationError):
        return False
    except Exception as error:  # noqa: BLE001 - cancellation must stay secret-safe
        logger.warning(
            "Cloud Image Pipeline remote cancellation failed error_type=%s",
            type(error).__name__,
        )
        return False


async def _completed_stream_stats(
    task: asyncio.Task[tuple[int, int]] | None,
) -> tuple[int, int]:
    """Join one drain task without letting a pipe failure expose or mask state."""

    if task is None:
        return 0, 0
    try:
        return await task
    except asyncio.CancelledError:
        return 0, 0
    except Exception as error:  # noqa: BLE001 - report only the fixed safe summary
        logger.warning(
            "Cloud Image Pipeline stream drain failed error_type=%s",
            type(error).__name__,
        )
        return 0, 0


async def _execution_cleanup_summary(
    *,
    process: asyncio.subprocess.Process | None,
    target: CloudImageSSHExecutionTarget,
    known_hosts_file: Path | None,
    identity: OpenSSHIdentityFile | None,
    remote_unit: str,
    stdout_task: asyncio.Task[tuple[int, int]] | None,
    stderr_task: asyncio.Task[tuple[int, int]] | None,
) -> CloudImageTemplateExecutionSummary:
    """Stop local/remote work and join stream drains as one mandatory task."""

    if process is not None:
        await _stop_process(process)
    cancellation_succeeded = bool(
        known_hosts_file is not None
        and identity is not None
        and await _cancel_remote_unit(target, known_hosts_file, identity, remote_unit)
    )
    stdout_stats, stderr_stats = await asyncio.gather(
        _completed_stream_stats(stdout_task),
        _completed_stream_stats(stderr_task),
    )
    return CloudImageTemplateExecutionSummary(
        attempted=True,
        enabled=True,
        stdout_bytes=stdout_stats[0],
        stderr_bytes=stderr_stats[0],
        stdout_lines=stdout_stats[1],
        stderr_lines=stderr_stats[1],
        cancellation_attempted=True,
        cancellation_succeeded=cancellation_succeeded,
    )


async def _await_execution_cleanup(
    *,
    process: asyncio.subprocess.Process | None,
    target: CloudImageSSHExecutionTarget,
    known_hosts_file: Path | None,
    identity: OpenSSHIdentityFile | None,
    remote_unit: str,
    stdout_task: asyncio.Task[tuple[int, int]] | None,
    stderr_task: asyncio.Task[tuple[int, int]] | None,
) -> tuple[CloudImageTemplateExecutionSummary, bool]:
    """Return cleanup state and whether cancellation arrived while it ran."""

    cleanup_task = asyncio.create_task(
        _execution_cleanup_summary(
            process=process,
            target=target,
            known_hosts_file=known_hosts_file,
            identity=identity,
            remote_unit=remote_unit,
            stdout_task=stdout_task,
            stderr_task=stderr_task,
        )
    )
    try:
        return await await_task_through_repeated_cancellation(cleanup_task), False
    except asyncio.CancelledError:
        if cleanup_task.cancelled():
            raise
        return cleanup_task.result(), True


async def _execution_summary_after_exit(
    *,
    returncode: int,
    stdout_task: asyncio.Task[tuple[int, int]] | None,
    stderr_task: asyncio.Task[tuple[int, int]] | None,
) -> CloudImageTemplateExecutionSummary:
    stdout_stats, stderr_stats = await asyncio.gather(
        _completed_stream_stats(stdout_task),
        _completed_stream_stats(stderr_task),
    )
    return CloudImageTemplateExecutionSummary(
        attempted=True,
        enabled=True,
        exit_code=returncode,
        stdout_bytes=stdout_stats[0],
        stderr_bytes=stderr_stats[0],
        stdout_lines=stdout_stats[1],
        stderr_lines=stderr_stats[1],
    )


async def cancel_pipeline_operation(
    target: CloudImageSSHExecutionTarget,
    *,
    remote_unit: str,
) -> bool:
    """Stop one server-generated remote unit with pinned host-key verification."""

    known_hosts_file: Path | None = None
    identity: OpenSSHIdentityFile | None = None
    try:
        identity = _open_ssh_identity(target)
        known_hosts_file = await _pinned_known_hosts_file(target)
        return await _cancel_remote_unit(target, known_hosts_file, identity, remote_unit)
    except (_SSHHostKeyVerificationError, _SSHIdentityFileVerificationError):
        return False
    finally:
        _remove_known_hosts_file(known_hosts_file)
        if identity is not None:
            identity.close()


async def _pipeline_execution_result(  # noqa: C901
    request: CloudImageTemplateBuildRequest,
    build_script: str,
    *,
    execution_allowed: bool,
    execution_target: CloudImageSSHExecutionTarget | None,
    remote_unit: str,
) -> tuple[
    str,
    int | None,
    CloudImageTemplateExecutionSummary,
    list[PackerFinding],
    str | None,
]:
    """Execute a rendered script and expose only bounded, secret-free metadata."""
    if not execution_allowed:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "cloud_image_execution_disabled",
                "message": "Remote Cloud Image Pipeline execution is disabled.",
            },
        )
    if execution_target is None:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "endpoint_ssh_binding_required",
                "message": "A complete persisted endpoint/node SSH binding is required.",
            },
        )

    known_hosts_file: Path | None = None
    identity: OpenSSHIdentityFile | None = None
    process: asyncio.subprocess.Process | None = None
    stdout_task: asyncio.Task[tuple[int, int]] | None = None
    stderr_task: asyncio.Task[tuple[int, int]] | None = None
    execution: CloudImageTemplateExecutionSummary | None = None
    completion_cancelled = False
    try:
        identity = _open_ssh_identity(execution_target)
        known_hosts_file = await _pinned_known_hosts_file(execution_target)
        process = await asyncio.create_subprocess_exec(
            *_ssh_argv(
                execution_target,
                known_hosts_file,
                identity,
                "/usr/bin/systemd-run",
                "--quiet",
                "--wait",
                "--pipe",
                "--collect",
                "--unit",
                remote_unit,
                "/bin/bash",
                "-s",
            ),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=(identity.fd,),
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise OSError("subprocess pipes unavailable")
        stdout_task = asyncio.create_task(_stream_stats(process.stdout))
        stderr_task = asyncio.create_task(_stream_stats(process.stderr))
        process.stdin.write(build_script.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
        await process.stdin.wait_closed()
        returncode = await asyncio.wait_for(process.wait(), 3600)
        summary_task = asyncio.create_task(
            _execution_summary_after_exit(
                returncode=returncode,
                stdout_task=stdout_task,
                stderr_task=stderr_task,
            )
        )
        try:
            execution = await await_task_through_repeated_cancellation(summary_task)
        except asyncio.CancelledError:
            if summary_task.cancelled():
                raise
            execution = summary_task.result()
            completion_cancelled = True
    except _SSHIdentityFileVerificationError:
        return (
            "failed",
            None,
            CloudImageTemplateExecutionSummary(attempted=False, enabled=True),
            [
                PackerFinding(
                    code="ssh_identity_untrusted",
                    severity=PackerFindingSeverity.error,
                    target=f"endpoint:{request.endpoint_id}",
                    message="The persisted SSH identity file failed local trust checks.",
                )
            ],
            "ssh_identity_untrusted",
        )
    except _SSHHostKeyVerificationError:
        return (
            "failed",
            None,
            CloudImageTemplateExecutionSummary(attempted=True, enabled=True),
            [
                PackerFinding(
                    code="ssh_host_key_unverified",
                    severity=PackerFindingSeverity.error,
                    target=f"endpoint:{request.endpoint_id}",
                    message="The persisted SSH host-key fingerprint could not be verified.",
                )
            ],
            "ssh_host_key_unverified",
        )
    except TimeoutError:
        cleanup, cancelled_during_cleanup = await _await_execution_cleanup(
            process=process,
            target=execution_target,
            known_hosts_file=known_hosts_file,
            identity=identity,
            remote_unit=remote_unit,
            stdout_task=stdout_task,
            stderr_task=stderr_task,
        )
        if cancelled_during_cleanup:
            raise PipelineExecutionCancelled(cleanup) from None
        return (
            "failed",
            None,
            cleanup,
            [
                PackerFinding(
                    code="execution_timeout",
                    severity=PackerFindingSeverity.error,
                    target=f"endpoint:{request.endpoint_id}",
                    message="Remote Cloud Image Pipeline execution exceeded its time limit.",
                )
            ],
            "execution_timeout",
        )
    except asyncio.CancelledError:
        cleanup, _cancelled_during_cleanup = await _await_execution_cleanup(
            process=process,
            target=execution_target,
            known_hosts_file=known_hosts_file,
            identity=identity,
            remote_unit=remote_unit,
            stdout_task=stdout_task,
            stderr_task=stderr_task,
        )
        raise PipelineExecutionCancelled(cleanup) from None
    except Exception as error:  # noqa: BLE001 - response and logs must never expose raw output
        cleanup, cancelled_during_cleanup = await _await_execution_cleanup(
            process=process,
            target=execution_target,
            known_hosts_file=known_hosts_file,
            identity=identity,
            remote_unit=remote_unit,
            stdout_task=stdout_task,
            stderr_task=stderr_task,
        )
        if cancelled_during_cleanup:
            raise PipelineExecutionCancelled(cleanup) from None
        logger.warning(
            "Cloud Image Pipeline execution could not start error_type=%s",
            type(error).__name__,
        )
        return (
            "failed",
            None,
            cleanup,
            [
                PackerFinding(
                    code="execution_unavailable",
                    severity=PackerFindingSeverity.error,
                    target=f"endpoint:{request.endpoint_id}",
                    message="Remote Cloud Image Pipeline execution could not be started.",
                )
            ],
            "execution_unavailable",
        )
    finally:
        _remove_known_hosts_file(known_hosts_file)
        if identity is not None:
            identity.close()

    if execution is None:
        raise RuntimeError("execution summary unavailable")
    if completion_cancelled:
        raise PipelineExecutionCancelled(execution) from None
    succeeded = returncode == 0
    diagnostic = PackerFinding(
        code="execution_awaiting_verification" if succeeded else "execution_failed",
        severity=PackerFindingSeverity.warning if succeeded else PackerFindingSeverity.error,
        target=f"endpoint:{request.endpoint_id}",
        message=(
            "Remote execution finished and is awaiting Proxmox artifact verification."
            if succeeded
            else "Remote Cloud Image Pipeline execution failed; inspect protected host logs."
        ),
    )
    return (
        "verification_pending" if succeeded else "failed",
        returncode,
        execution,
        [diagnostic],
        None if succeeded else "execution_failed",
    )


def _render_pipeline(
    request: CloudImageTemplateBuildRequest,
) -> tuple[
    CloudImageTemplateBuildRequest,
    CloudImageVersionEntry,
    CloudImageBuildProvider,
    str | None,
    str | None,
    str,
    list[str],
    str | None,
]:
    entry, provider, user_data, first_boot_script = _resolve_pipeline_inputs(request)
    if "vm_storage" not in request.model_fields_set:
        request = request.model_copy(update={"vm_storage": _LEGACY_PIPELINE_VM_STORAGE})
    build_target = request.build_target(provider=provider)
    request = request.model_copy(
        update={
            "target_node": build_target.target_node,
            "image_storage": build_target.image_storage,
            "vm_storage": build_target.vm_storage,
            "snippets_storage": build_target.snippets_storage or request.snippets_storage,
        }
    )

    if provider == CloudImageBuildProvider.source_tree:
        build_script, commands = _source_tree_script(request, entry, first_boot_script)
        image_url = _catalog_image_url(request, entry)
    elif provider == CloudImageBuildProvider.proxmox_iso:
        build_script, commands, image_url = _proxmox_iso_script(request, entry)
    else:
        build_script, commands, image_url = _release_image_script(
            request,
            entry,
            first_boot_script,
            user_data,
        )
    return (
        request,
        entry,
        provider,
        user_data,
        first_boot_script,
        build_script,
        commands,
        image_url,
    )


def _recipe_binding_digest(build_script: str) -> str:
    """Authenticate a recipe without exposing a dictionary-testable hash."""

    return hmac.new(
        derive_service_signing_key(_RECIPE_BINDING_CONTEXT),
        build_script.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def pipeline_recipe_digest(request: CloudImageTemplateBuildRequest) -> str:
    """Return the opaque binding for the exact execution-independent recipe."""

    planned_request = request.model_copy(
        update={"execute": False, "include_sensitive_preview": False, "preflight_plan_token": None}
    )
    *_unused, build_script, _commands, _image_url = _render_pipeline(planned_request)
    return _recipe_binding_digest(build_script)


def pipeline_execution_contract(
    request: CloudImageTemplateBuildRequest,
) -> tuple[CloudImageBuildTarget, str]:
    """Return the normalized target and exact recipe digest used by execution."""

    planned_request = request.model_copy(
        update={"execute": False, "include_sensitive_preview": False, "preflight_plan_token": None}
    )
    normalized, _entry, provider, _user_data, _first_boot, script, _commands, _url = (
        _render_pipeline(planned_request)
    )
    return normalized.build_target(provider=provider), _recipe_binding_digest(script)


def _pipeline_response(
    request: CloudImageTemplateBuildRequest,
    *,
    entry: CloudImageVersionEntry,
    provider: CloudImageBuildProvider,
    user_data: str | None,
    first_boot_script: str | None,
    build_script: str,
    commands: list[str],
    image_url: str | None,
    status: str,
    returncode: int | None,
    execution: CloudImageTemplateExecutionSummary,
    diagnostics: list[PackerFinding],
    operation_id: str | None = None,
) -> CloudImageTemplateBuildResponse:
    execution_allowed = os.environ.get("PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION", "").lower() in {
        "1",
        "true",
        "yes",
    }
    recipe_digest = _recipe_binding_digest(build_script)
    sensitive_preview = None
    if request.include_sensitive_preview:
        source_command = entry.source_build_command
        sensitive_preview = CloudImageTemplateSensitivePreview(
            image_url=image_url,
            source_tree_path=(source_command.source_root if source_command else None),
            source_artifact_path=(
                f"{source_command.source_root}/{source_command.artifact_relative_path}"
                if source_command
                else None
            ),
            generated_userdata=user_data,
            first_boot_script=first_boot_script,
            build_script=build_script,
            commands=commands,
        )

    return CloudImageTemplateBuildResponse(
        status=status,
        endpoint_id=request.endpoint_id,
        target_node=request.target_node,
        vmid=request.vmid,
        template_vmid=request.vmid,
        name=request.name,
        product_type=entry.product_type,
        product_version=entry.version,
        provider=provider,
        recipe_digest=recipe_digest,
        operation_id=operation_id,
        image_volid="",
        operator_instructions=(
            "Submit recipe_digest to the endpoint-scoped preflight, then present its signed "
            "plan_token for execution before it expires. Raw output remains in protected host logs."
        ),
        execution_enabled=execution_allowed,
        returncode=returncode,
        execution=execution,
        diagnostics=diagnostics,
        sensitive_preview=sensitive_preview,
    )


def build_pipeline_response(
    request: CloudImageTemplateBuildRequest,
    *,
    execution_target: CloudImageSSHExecutionTarget | None = None,
) -> CloudImageTemplateBuildResponse:
    if request.execute:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "bound_async_execution_required",
                "message": "Executable builds require the bound asynchronous route workflow.",
            },
        )
    del execution_target  # compatibility-only parameter; plans never consume SSH authority
    (
        request,
        entry,
        provider,
        user_data,
        first_boot_script,
        build_script,
        commands,
        image_url,
    ) = _render_pipeline(request)
    execution_allowed = os.environ.get("PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION", "").lower() in {
        "1",
        "true",
        "yes",
    }
    return _pipeline_response(
        request,
        entry=entry,
        provider=provider,
        user_data=user_data,
        first_boot_script=first_boot_script,
        build_script=build_script,
        commands=commands,
        image_url=image_url,
        status="planned",
        returncode=None,
        execution=CloudImageTemplateExecutionSummary(enabled=execution_allowed),
        diagnostics=[
            PackerFinding(
                code="pipeline_planned",
                severity=PackerFindingSeverity.info,
                target=f"vmid:{request.vmid}",
                message="The Cloud Image Pipeline plan was rendered without execution.",
            )
        ],
    )


async def execute_pipeline_response(
    request: CloudImageTemplateBuildRequest,
    *,
    execution_target: CloudImageSSHExecutionTarget,
    operation_id: str,
    remote_unit: str,
) -> tuple[CloudImageTemplateBuildResponse, str | None]:
    """Run one leased operation; completion still requires API verification."""

    (
        request,
        entry,
        provider,
        user_data,
        first_boot_script,
        build_script,
        commands,
        image_url,
    ) = _render_pipeline(request)
    execution_allowed = os.environ.get("PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION", "").lower() in {
        "1",
        "true",
        "yes",
    }
    status_value, returncode, execution, diagnostics, error_code = await _pipeline_execution_result(
        request,
        build_script,
        execution_allowed=execution_allowed,
        execution_target=execution_target,
        remote_unit=remote_unit,
    )
    return (
        _pipeline_response(
            request,
            entry=entry,
            provider=provider,
            user_data=user_data,
            first_boot_script=first_boot_script,
            build_script=build_script,
            commands=commands,
            image_url=image_url,
            status=status_value,
            returncode=returncode,
            execution=execution,
            diagnostics=diagnostics,
            operation_id=operation_id,
        ),
        error_code,
    )

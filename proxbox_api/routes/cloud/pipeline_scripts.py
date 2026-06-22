"""Script-rendered Cloud Image Build Pipeline helpers.

The existing ``template_images`` route can create generic cloud-image templates
through the Proxmox API. These helpers cover the operator-driven path used by
NMS and netbox-proxbox when the source is a pfSense/OPNsense release image or
an appliance source tree that must be built on a Proxmox-capable host.
"""

from __future__ import annotations

import os
import shlex
import subprocess

from fastapi import HTTPException

from proxbox_api.routes.cloud.catalog import find_product_version
from proxbox_api.schemas.cloud_provision import (
    CloudImageBuildProvider,
    CloudImageTemplateBuildRequest,
    CloudImageTemplateBuildResponse,
    CloudImageVersionEntry,
)


def _q(value: object) -> str:
    return shlex.quote(str(value))


def _storage(request: CloudImageTemplateBuildRequest) -> str:
    return request.storage or request.vm_storage


def _target_node(request: CloudImageTemplateBuildRequest) -> str:
    return request.target_node or request.ssh_host or "pve-host"


def _artifact_from_url(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name or "cloud-image-artifact"


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
    return f"""#!/bin/sh
set -eu
MARKER="/var/db/nmulticloud-cloud-image-bootstrap.done"
[ -f "$MARKER" ] && exit 0

mkdir -p /usr/local/etc/nmulticloud /root/.ssh /var/db
chmod 700 /root/.ssh
cat > /usr/local/etc/nmulticloud/cloud-image.env <<'EOF'
PIPELINE_NAME="Cloud Image Build Pipeline"
PRODUCT="{entry.product_type.value}"
PRODUCT_VERSION="{entry.version}"
HOSTNAME="{request.hostname}"
DOMAIN="{request.domain}"
NODE_CIDR="{request.node_cidr or ""}"
GATEWAY="{request.gateway or ""}"
NAMESERVERS="{nameservers}"
EOF

cat > /root/.ssh/authorized_keys <<'EOF'
{ssh_keys}
EOF
chmod 600 /root/.ssh/authorized_keys

hostname "{request.hostname}"
echo "{request.hostname}" > /etc/myname 2>/dev/null || true
touch "$MARKER"
"""


def _append_template_import_commands(
    commands: list[str],
    request: CloudImageTemplateBuildRequest,
    entry: CloudImageVersionEntry,
    qcow_path: str,
    first_boot_script: str | None,
    user_data: str | None,
) -> None:
    snippet_name = f"{request.name}-{entry.product_type.value}-{entry.version}".replace("/", "-")
    storage = _storage(request)
    commands.extend(
        [
            (
                f"qm create {_q(request.vmid)} --name {_q(request.name)} "
                f"--memory {_q(request.memory_mb)} --cores {_q(request.cores)} "
                f"--net0 {_q('virtio,bridge=' + request.bridge)}"
            ),
            f"qm importdisk {_q(request.vmid)} {_q(qcow_path)} {_q(storage)}",
            (
                f"IMPORTED_VOLID=$(pvesm list {_q(storage)} --vmid {_q(request.vmid)} "
                "| awk 'NR == 2 {print $1}')"
            ),
            'test -n "${IMPORTED_VOLID}"',
            f"qm set {_q(request.vmid)} --scsihw virtio-scsi-pci --scsi0 " + '"${IMPORTED_VOLID}"',
            f"qm set {_q(request.vmid)} --ide2 {_q(request.image_storage + ':cloudinit')}",
            f"qm set {_q(request.vmid)} --serial0 socket --vga serial0",
            f"qm set {_q(request.vmid)} --boot order=scsi0",
            f"qm set {_q(request.vmid)} --agent enabled=1",
        ]
    )
    if request.disk_size_gb:
        commands.append(f"qm resize {_q(request.vmid)} scsi0 {_q(str(request.disk_size_gb) + 'G')}")

    snippet_refs: list[str] = []
    if user_data:
        user_snippet = f"{request.snippets_dir}/{snippet_name}-user-data.yml"
        commands.append(f"cat > {_q(user_snippet)} <<'EOF_USER_DATA'\n{user_data}EOF_USER_DATA")
        snippet_refs.append(
            f"user={request.snippets_storage}:snippets/{snippet_name}-user-data.yml"
        )
    if first_boot_script:
        boot_snippet = f"{request.snippets_dir}/{snippet_name}-first-boot.sh"
        commands.append(
            f"cat > {_q(boot_snippet)} <<'EOF_FIRST_BOOT'\n{first_boot_script}EOF_FIRST_BOOT"
        )
        commands.append(f"chmod 600 {_q(boot_snippet)}")
        snippet_refs.append(
            f"vendor={request.snippets_storage}:snippets/{snippet_name}-first-boot.sh"
        )
    meta_snippet = f"{request.snippets_dir}/{snippet_name}-meta-data.yml"
    meta_data = f"instance-id: {snippet_name}\nlocal-hostname: {request.hostname}\n"
    commands.append(f"cat > {_q(meta_snippet)} <<'EOF_META'\n{meta_data}EOF_META")
    snippet_refs.append(f"meta={request.snippets_storage}:snippets/{snippet_name}-meta-data.yml")
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
    image_url = request.image_url or entry.image_url
    if not image_url:
        raise HTTPException(
            status_code=422, detail="image_url is required for release image builds."
        )
    filename = _artifact_from_url(image_url)
    cache_path = f"/var/lib/vz/template/cache/{filename}"
    raw_path = f"/var/lib/vz/template/cache/{_decompressed_name(filename, entry.compression)}"
    qcow_path = f"/var/lib/vz/template/cache/{request.name}-{entry.version}.qcow2"
    commands = [
        "set -euo pipefail",
        "install -d /var/lib/vz/template/cache",
        f"install -d {_q(request.snippets_dir)}",
        f"curl -fL --retry 3 -o {_q(cache_path)} {_q(image_url)}",
    ]
    sha256 = request.sha256 or entry.sha256
    if sha256:
        commands.append(f"printf '%s  %s\\n' {_q(sha256)} {_q(cache_path)} | sha256sum -c -")
    decompress = _decompress_command(cache_path, entry.compression)
    if decompress:
        commands.append(decompress)
    commands.append(f"qemu-img convert -O qcow2 {_q(raw_path)} {_q(qcow_path)}")
    _append_template_import_commands(
        commands, request, entry, qcow_path, first_boot_script, user_data
    )
    return "\n".join(commands) + "\n", commands, image_url


def _source_tree_script(
    request: CloudImageTemplateBuildRequest,
    entry: CloudImageVersionEntry,
    first_boot_script: str | None,
) -> tuple[str, list[str]]:
    source_tree = request.source_tree_path or entry.source_tree_path
    build_command = request.source_build_command or entry.source_build_command
    if not source_tree:
        raise HTTPException(status_code=422, detail="source_tree_path is required.")
    commands = [
        "set -euo pipefail",
        "install -d /var/lib/vz/template/cache",
        f"install -d {_q(request.snippets_dir)}",
        f"cd {_q(source_tree)}",
    ]
    if build_command:
        commands.append(build_command)
    if request.source_artifact_path:
        qcow_path = f"/var/lib/vz/template/cache/{request.name}-{entry.version}.qcow2"
        commands.extend(
            [
                f"test -f {_q(request.source_artifact_path)}",
                f"qemu-img convert -O qcow2 {_q(request.source_artifact_path)} {_q(qcow_path)}",
            ]
        )
        _append_template_import_commands(
            commands, request, entry, qcow_path, first_boot_script, None
        )
    else:
        commands.append(
            "# Set source_artifact_path to the image emitted by the source build to import it."
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
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    provider = request.provider or entry.default_provider
    if provider not in entry.supported_providers:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{provider.value} is not supported for {entry.product_type.value} {entry.version}."
            ),
        )

    user_data: str | None = None
    first_boot_script: str | None = None
    if entry.product_type.value == "firecracker":
        from proxbox_api.routes.cloud.cloud_init_templates import generate_firecracker_userdata

        user_data = generate_firecracker_userdata(
            os_family=entry.os_family,
            os_codename=entry.os_codename or entry.debian_codename or request.debian_release,
        )
    elif entry.product_type.value in {"pve", "pbs", "pdm"}:
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


def build_pipeline_response(
    request: CloudImageTemplateBuildRequest,
) -> CloudImageTemplateBuildResponse:
    entry, provider, user_data, first_boot_script = _resolve_pipeline_inputs(request)

    image_url = request.image_url or entry.image_url
    if provider == CloudImageBuildProvider.SOURCE_TREE:
        build_script, commands = _source_tree_script(request, entry, first_boot_script)
    else:
        build_script, commands, image_url = _release_image_script(
            request,
            entry,
            first_boot_script,
            user_data,
        )

    execution_allowed = os.environ.get("PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION", "").lower() in {
        "1",
        "true",
        "yes",
    }
    status = "planned"
    returncode = None
    stdout = None
    stderr = None
    if request.execute:
        if not execution_allowed:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Remote Cloud Image Build Pipeline execution is disabled. "
                    "Set PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION=true to enable it."
                ),
            )
        if not request.ssh_host:
            raise HTTPException(status_code=422, detail="ssh_host is required when execute=true.")
        ssh_command = ["ssh", "-p", str(request.ssh_port)]
        if request.ssh_identity_file:
            ssh_command.extend(["-i", request.ssh_identity_file])
        ssh_command.extend([f"{request.ssh_user}@{request.ssh_host}", "bash -s"])
        proc = subprocess.run(
            ssh_command,
            input=build_script,
            text=True,
            capture_output=True,
            check=False,
            timeout=3600,
        )
        status = "completed" if proc.returncode == 0 else "failed"
        returncode = proc.returncode
        stdout = proc.stdout
        stderr = proc.stderr

    return CloudImageTemplateBuildResponse(
        status=status,
        endpoint_id=request.endpoint_id,
        target_node=_target_node(request),
        vmid=request.vmid,
        template_vmid=request.vmid,
        name=request.name,
        product_type=entry.product_type,
        product_version=entry.version,
        provider=provider,
        image_url=image_url,
        image_volid="",
        source_tree_path=request.source_tree_path or entry.source_tree_path,
        source_artifact_path=request.source_artifact_path,
        generated_userdata=user_data,
        first_boot_script=first_boot_script,
        build_script=build_script,
        commands=commands,
        operator_instructions=(
            "Review the generated Cloud Image Build Pipeline script, enable snippets on the "
            "target Proxmox storage, then rerun with execute=true after remote execution is enabled."
        ),
        execution_enabled=execution_allowed,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )

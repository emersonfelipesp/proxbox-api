"""Cloud Image Build Pipeline routes."""

from __future__ import annotations

import os
import shlex
import subprocess
from textwrap import dedent

from fastapi import APIRouter, HTTPException

from proxbox_api.routes.cloud.catalog import catalog_payload, find_product_version
from proxbox_api.schemas.cloud_provision import (
    CloudImageBuildProvider,
    CloudImageProductType,
    CloudImageTemplateBuildRequest,
    CloudImageTemplateBuildResponse,
    CloudImageVersionEntry,
)

router = APIRouter()


def _q(value: object) -> str:
    return shlex.quote(str(value))


def generate_cloud_init_userdata(
    product_type: CloudImageProductType,
    entry: CloudImageVersionEntry,
    request: CloudImageTemplateBuildRequest,
) -> str:
    """Generate Debian cloud-init for PVE/PBS/PDM package-installer templates."""
    codename = entry.debian_codename or request.debian_release
    package_name = entry.package_name or "proxmox-ve"
    repo_suite = entry.repo_suite or product_type.value
    repo_component = entry.repo_component or f"{repo_suite}-no-subscription"
    service_name = entry.service_name or package_name
    pin = request.pve_version_pin
    write_files = [
        (
            f"  - path: /etc/apt/sources.list.d/{product_type.value}-install-repo.list\n"
            "    content: |\n"
            f"      deb [arch=amd64] http://download.proxmox.com/debian/{repo_suite} "
            f"{codename} {repo_component}"
        )
    ]
    if pin:
        write_files.append(
            f"""\
  - path: /etc/apt/preferences.d/nmulticloud-{product_type.value}-pin
    content: |
      Package: {package_name}
      Pin: version {pin}*
      Pin-Priority: 1001
"""
        )
    ssh_keys = "\n".join(f"    - {key}" for key in request.ssh_authorized_keys)
    ssh_block = f"\nssh_authorized_keys:\n{ssh_keys}" if ssh_keys else ""
    write_files_block = "\n".join(item.rstrip() for item in write_files)
    return f"""#cloud-config
hostname: {request.hostname}
fqdn: {request.hostname}.{request.domain}
package_update: true
package_upgrade: true
write_files:
{write_files_block}
runcmd:
  - curl -fsSL -o /etc/apt/trusted.gpg.d/proxmox-release-{codename}.gpg https://enterprise.proxmox.com/debian/proxmox-release-{codename}.gpg
  - rm -f /etc/apt/sources.list.d/pve-enterprise.list /etc/apt/sources.list.d/pbs-enterprise.list
  - DEBIAN_FRONTEND=noninteractive apt-get update
  - DEBIAN_FRONTEND=noninteractive apt-get install -y {package_name}
  - systemctl enable {service_name}
power_state:
  mode: poweroff
  condition: true
{ssh_block}
"""


def generate_appliance_first_boot_script(
    entry: CloudImageVersionEntry,
    request: CloudImageTemplateBuildRequest,
) -> str:
    """Generate the N-MultiCloud first-boot appliance bootstrap script."""
    nameservers = " ".join(request.nameservers)
    ssh_keys = "\n".join(request.ssh_authorized_keys)
    return dedent(
        f"""\
        #!/bin/sh
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
    ).strip() + "\n"


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


def _append_template_import_commands(
    commands: list[str],
    request: CloudImageTemplateBuildRequest,
    entry: CloudImageVersionEntry,
    qcow_path: str,
    first_boot_script: str | None,
    user_data: str | None,
) -> None:
    snippet_name = f"{request.name}-{entry.product_type.value}-{entry.version}".replace("/", "-")
    commands.extend(
        [
            (
                f"qm create {_q(request.vmid)} --name {_q(request.name)} "
                f"--memory {_q(request.memory_mb)} --cores {_q(request.cores)} "
                f"--net0 {_q('virtio,bridge=' + request.bridge)}"
            ),
            f"qm importdisk {_q(request.vmid)} {_q(qcow_path)} {_q(request.storage)}",
            (
                "IMPORTED_VOLID=$(pvesm list "
                f"{_q(request.storage)} --vmid {_q(request.vmid)} "
                "| awk 'NR == 2 {print $1}')"
            ),
            'test -n "${IMPORTED_VOLID}"',
            f"qm set {_q(request.vmid)} --scsihw virtio-scsi-pci --scsi0 " + '"${IMPORTED_VOLID}"',
            f"qm set {_q(request.vmid)} --ide2 {_q(request.image_storage + ':cloudinit')}",
            f"qm set {_q(request.vmid)} --serial0 socket --vga serial0",
            f"qm set {_q(request.vmid)} --boot order=scsi0",
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
    if snippet_refs:
        commands.append(f"qm set {_q(request.vmid)} --cicustom {_q(','.join(snippet_refs))}")
    commands.append(
        f"qm set {_q(request.vmid)} --description {_q('Built by Cloud Image Build Pipeline')}"
    )
    commands.append(f"qm template {_q(request.vmid)}")


def _build_release_image_script(
    request: CloudImageTemplateBuildRequest,
    entry: CloudImageVersionEntry,
    first_boot_script: str | None,
    user_data: str | None,
) -> tuple[str, list[str], str | None]:
    image_url = request.image_url or request.debian_image_url or entry.image_url
    if not image_url:
        raise HTTPException(
            status_code=422,
            detail="image_url is required for release_image/debian_cloud_image builds.",
        )
    filename = _artifact_from_url(image_url)
    cache_path = f"/var/lib/vz/template/cache/{filename}"
    raw_name = _decompressed_name(filename, entry.compression)
    raw_path = f"/var/lib/vz/template/cache/{raw_name}"
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
        commands,
        request,
        entry,
        qcow_path,
        first_boot_script,
        user_data,
    )
    return "\n".join(commands) + "\n", commands, image_url


def _build_source_tree_script(
    request: CloudImageTemplateBuildRequest,
    entry: CloudImageVersionEntry,
    first_boot_script: str | None,
) -> tuple[str, list[str]]:
    source_tree = request.source_tree_path or entry.source_tree_path
    build_command = request.source_build_command or entry.source_build_command
    artifact = request.source_artifact_path
    if not source_tree:
        raise HTTPException(status_code=422, detail="source_tree_path is required.")
    if not build_command and not artifact:
        raise HTTPException(
            status_code=422,
            detail="source_build_command or source_artifact_path is required for source_tree builds.",
        )
    commands = [
        "set -euo pipefail",
        "install -d /var/lib/vz/template/cache",
        f"install -d {_q(request.snippets_dir)}",
        f"cd {_q(source_tree)}",
    ]
    if build_command:
        commands.append(build_command)
    if artifact:
        qcow_path = f"/var/lib/vz/template/cache/{request.name}-{entry.version}.qcow2"
        commands.extend(
            [
                f"test -f {_q(artifact)}",
                f"qemu-img convert -O qcow2 {_q(artifact)} {_q(qcow_path)}",
            ]
        )
        _append_template_import_commands(
            commands,
            request,
            entry,
            qcow_path,
            first_boot_script,
            None,
        )
    else:
        commands.append(
            "# Set source_artifact_path to the image emitted by the source build to import it."
        )
    return "\n".join(commands) + "\n", commands


def build_pipeline_response(
    request: CloudImageTemplateBuildRequest,
) -> CloudImageTemplateBuildResponse:
    """Build the pipeline response and optionally execute it over SSH."""
    entry = find_product_version(request.product_type, request.product_version)
    provider = request.provider or entry.default_provider
    if provider not in entry.supported_providers:
        raise HTTPException(
            status_code=422,
            detail=f"{provider.value} is not supported for {entry.product_type.value} {entry.version}.",
        )

    user_data = None
    first_boot_script = None
    image_url = request.image_url or request.debian_image_url or entry.image_url
    source_tree = request.source_tree_path or entry.source_tree_path
    source_artifact = request.source_artifact_path

    if entry.product_type in {
        CloudImageProductType.PVE,
        CloudImageProductType.PBS,
        CloudImageProductType.PDM,
    }:
        user_data = generate_cloud_init_userdata(entry.product_type, entry, request)
    else:
        first_boot_script = generate_appliance_first_boot_script(entry, request)

    if provider == CloudImageBuildProvider.SOURCE_TREE:
        build_script, commands = _build_source_tree_script(request, entry, first_boot_script)
    else:
        build_script, commands, image_url = _build_release_image_script(
            request, entry, first_boot_script, user_data
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
        product_type=entry.product_type,
        product_version=entry.version,
        provider=provider,
        endpoint_id=request.endpoint_id,
        target_node=request.target_node,
        vmid=request.vmid,
        template_vmid=request.vmid,
        name=request.name,
        image_url=image_url,
        source_tree_path=source_tree,
        source_artifact_path=source_artifact,
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


@router.get("/templates/versions")
async def get_cloud_template_versions() -> dict[str, list[dict[str, object]]]:
    return catalog_payload()


@router.post("/templates/images", response_model=CloudImageTemplateBuildResponse)
async def build_cloud_template_image(
    request: CloudImageTemplateBuildRequest,
) -> CloudImageTemplateBuildResponse:
    return build_pipeline_response(request)


@router.post("/templates/pve", response_model=CloudImageTemplateBuildResponse)
async def build_pve_template(
    request: CloudImageTemplateBuildRequest,
) -> CloudImageTemplateBuildResponse:
    request.product_type = CloudImageProductType.PVE
    if request.provider is None:
        request.provider = CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE
    return build_pipeline_response(request)

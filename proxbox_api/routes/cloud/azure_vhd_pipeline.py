"""Azure VHD import planning helpers."""

from __future__ import annotations

import asyncio
import os
import shlex
from collections.abc import Awaitable, Callable
from pathlib import PurePosixPath
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import HTTPException

from proxbox_api.routes.cloud.pipeline_scripts import (
    PipelineExecutionCancelled,
    execute_remote_script,
)
from proxbox_api.schemas.cloud_image_security import CloudImageSSHExecutionTarget
from proxbox_api.schemas.cloud_provision import (
    AzureVhdGuestProfile,
    AzureVhdImportRequest,
    AzureVhdImportResponse,
    AzureVmGeneration,
    CloudImageTemplateExecutionSummary,
    PackerFinding,
)


def _q(value: object) -> str:
    return shlex.quote(str(value))


def _source_filename(request: AzureVhdImportRequest) -> str:
    raw = (request.source_vhd_filename or "").strip()
    if not raw:
        raw = PurePosixPath(urlsplit(request.azure_vhd_url).path).name
    raw = PurePosixPath(raw).name
    if not raw:
        raw = f"{request.name}.vhd"
    if "." not in raw:
        raw = f"{raw}.vhd"
    return raw


def _qcow_filename(source_vhd_filename: str) -> str:
    stem = PurePosixPath(source_vhd_filename).stem or "azure-import"
    return f"{stem}.qcow2"


def _bios(vm_generation: AzureVmGeneration) -> str:
    return "ovmf" if vm_generation == AzureVmGeneration.gen2 else "seabios"


def _machine(vm_generation: AzureVmGeneration) -> str | None:
    return "q35" if vm_generation == AzureVmGeneration.gen2 else None


def _disk_interface(guest_profile: AzureVhdGuestProfile) -> str:
    if guest_profile == AzureVhdGuestProfile.windows_first_boot_safe:
        return "sata0"
    return "scsi0"


def _network_model(guest_profile: AzureVhdGuestProfile) -> str:
    if guest_profile == AzureVhdGuestProfile.windows_first_boot_safe:
        return "e1000"
    return "virtio"


def _boot_order(disk_interface: str) -> str:
    return disk_interface


def _net0_arg(request: AzureVhdImportRequest) -> str:
    value = f"{_network_model(request.guest_profile)},bridge={request.bridge}"
    if request.vlan_tag is not None:
        value = f"{value},tag={request.vlan_tag}"
    return value


def _follow_up_steps(request: AzureVhdImportRequest) -> list[str]:
    if request.guest_profile == AzureVhdGuestProfile.windows_first_boot_safe:
        return [
            "Boot the VM on an isolated VLAN first and verify Windows storage/network drivers.",
            "Install VirtIO storage, network, balloon, and QEMU guest agent drivers inside Windows.",
            "After driver installation, switch the imported disk from SATA to SCSI/VirtIO if desired.",
            "Validate activation, application services, and RDP before exposing production traffic.",
        ]
    return [
        "Boot the VM on an isolated VLAN first and verify network, filesystem, and service health.",
        "Install or enable the QEMU guest agent inside the guest after the first successful boot.",
        "Replace any Azure-specific network or metadata assumptions before production cutover.",
    ]


async def _execution_result(
    request: AzureVhdImportRequest,
    execution_target: CloudImageSSHExecutionTarget | None,
    build_script: str,
    *,
    execution_allowed: bool,
    authorize_execution: Callable[[], Awaitable[None]] | None,
) -> tuple[
    str,
    int | None,
    CloudImageTemplateExecutionSummary,
    list[PackerFinding],
    bool,
]:
    """Return the planned state or execute through persisted SSH authority."""

    if not request.execute:
        return (
            "planned",
            None,
            CloudImageTemplateExecutionSummary(enabled=execution_allowed),
            [],
            False,
        )
    if not execution_allowed:
        raise HTTPException(
            status_code=403,
            detail=(
                "Remote Azure VHD import execution is disabled. "
                "Set PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION=true to enable it."
            ),
        )
    if execution_target is None:
        raise HTTPException(status_code=422, detail="Persisted endpoint authority is required.")
    try:
        status, returncode, execution, diagnostics, error_code = await execute_remote_script(
            request,
            build_script,
            execution_allowed=True,
            execution_target=execution_target,
            remote_unit=f"proxbox-azure-vhd-{uuid4().hex}",
            authorize_execution=authorize_execution,
        )
    except PipelineExecutionCancelled as cancelled:
        raise asyncio.CancelledError from cancelled
    recovery_required = _recovery_required(error_code, execution)
    return status, returncode, execution, diagnostics, recovery_required


def _recovery_required(
    error_code: str | None,
    execution: CloudImageTemplateExecutionSummary,
) -> bool:
    """Classify whether remote mutation state remains uncertain or partial."""

    if error_code in {None, "ssh_identity_untrusted", "ssh_host_key_unverified"}:
        return False
    if error_code == "execution_failed":
        return True
    if error_code in {"execution_timeout", "execution_unavailable"}:
        return bool(
            execution.cancellation_attempted and execution.cancellation_succeeded is not True
        )
    return execution.attempted


async def build_azure_vhd_import_response(
    request: AzureVhdImportRequest,
    *,
    execution_target: CloudImageSSHExecutionTarget | None = None,
    authorize_execution: Callable[[], Awaitable[None]] | None = None,
) -> AzureVhdImportResponse:
    source_vhd_filename = _source_filename(request)
    qcow2_filename = _qcow_filename(source_vhd_filename)
    source_vhd_path = f"/var/lib/vz/template/cache/{source_vhd_filename}"
    qcow2_path = f"/var/lib/vz/template/cache/{qcow2_filename}"
    bios = _bios(request.vm_generation)
    machine = _machine(request.vm_generation)
    disk_interface = _disk_interface(request.guest_profile)
    network_model = _network_model(request.guest_profile)
    boot_order = _boot_order(disk_interface)

    commands = [
        "set -euo pipefail",
        "command -v curl >/dev/null",
        "command -v qemu-img >/dev/null",
        "command -v qm >/dev/null",
        "command -v pvesm >/dev/null",
        f'test "$(hostname -s)" = {_q(request.target_node)}',
        f"! qm status {_q(request.vmid)} >/dev/null 2>&1",
        f"pvesm status --storage {_q(request.vm_storage)} >/dev/null",
        f"test -d /sys/class/net/{_q(request.bridge)}",
        "install -d /var/lib/vz/template/cache",
        f"curl -fL --retry 3 -C - -o {_q(source_vhd_path)} {_q(request.azure_vhd_url)}",
        f"qemu-img info {_q(source_vhd_path)} >/dev/null",
        f"qemu-img convert -f vpc -O qcow2 {_q(source_vhd_path)} {_q(qcow2_path)}",
        f"qemu-img info {_q(qcow2_path)} >/dev/null",
    ]

    qm_create = [
        "qm",
        "create",
        str(request.vmid),
        "--name",
        request.name,
        "--memory",
        str(request.memory_mb),
        "--cores",
        str(request.cores),
        "--cpu",
        request.cpu,
        "--net0",
        _net0_arg(request),
    ]
    if machine:
        qm_create.extend(["--machine", machine])
    qm_create.extend(["--bios", bios])
    commands.append(" ".join(_q(part) for part in qm_create))
    if request.enable_agent:
        commands.append(f"qm set {_q(request.vmid)} --agent enabled=1")
    if request.description:
        commands.append(f"qm set {_q(request.vmid)} --description {_q(request.description)}")
    if request.vm_generation == AzureVmGeneration.gen2:
        commands.append(
            f"qm set {_q(request.vmid)} --efidisk0 "
            f"{_q(request.vm_storage + ':0,efitype=4m,pre-enrolled-keys=0')}"
        )

    commands.extend(
        [
            "IMPORT_OUTPUT=$("
            f"qm importdisk {_q(request.vmid)} {_q(qcow2_path)} {_q(request.vm_storage)} "
            "--format qcow2 2>&1)",
            'printf "%s\\n" "${IMPORT_OUTPUT}"',
            (
                "IMPORTED_VOLID=$(printf '%s\\n' \"${IMPORT_OUTPUT}\" "
                '| sed -n "s/.*Successfully imported disk as '
                "'\\([^']*\\)'.*/\\1/p\" | tail -1)"
            ),
            'test -n "${IMPORTED_VOLID}"',
        ]
    )

    if disk_interface == "scsi0":
        commands.append(f"qm set {_q(request.vmid)} --scsihw virtio-scsi-single")
        commands.append(
            f"qm set {_q(request.vmid)} --scsi0 " + '"${IMPORTED_VOLID}",discard=on,iothread=1'
        )
    else:
        commands.append(f"qm set {_q(request.vmid)} --sata0 " + '"${IMPORTED_VOLID}"')

    commands.append(f"qm set {_q(request.vmid)} --boot {_q('order=' + boot_order)}")
    build_script = "\n".join(commands) + "\n"

    execution_allowed = os.environ.get("PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION", "").lower() in {
        "1",
        "true",
        "yes",
    }
    status, returncode, execution, diagnostics, recovery_required = await _execution_result(
        request,
        execution_target,
        build_script,
        execution_allowed=execution_allowed,
        authorize_execution=authorize_execution,
    )
    executed = bool(request.execute)

    return AzureVhdImportResponse(
        status=status,
        endpoint_id=request.endpoint_id,
        target_node=request.target_node,
        vmid=request.vmid,
        name=request.name,
        vm_generation=request.vm_generation,
        guest_profile=request.guest_profile,
        bios=bios,
        machine=machine,
        disk_interface=disk_interface,
        network_model=network_model,
        boot_order=boot_order,
        azure_vhd_url="" if executed else request.azure_vhd_url,
        source_vhd_filename=source_vhd_filename,
        source_vhd_path=source_vhd_path,
        qcow2_filename=qcow2_filename,
        qcow2_path=qcow2_path,
        build_script="" if executed else build_script,
        commands=[] if executed else commands,
        follow_up_steps=_follow_up_steps(request),
        operator_instructions=(
            "Review the generated Azure VHD Import Pipeline script, verify the target "
            "storage and bridge on the Proxmox host, and rerun with execute=true only "
            "after remote execution is explicitly enabled."
        ),
        execution_enabled=execution_allowed,
        returncode=returncode,
        stdout=None,
        stderr=None,
        execution=execution,
        diagnostics=diagnostics,
        recovery_required=recovery_required,
    )

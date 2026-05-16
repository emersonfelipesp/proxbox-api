"""PVE-installer cloud-init template build route.

Sibling of ``template_images.py``. Where that endpoint builds a generic
Ubuntu/Debian cloud-init template, this one builds a template whose first
boot installs Proxmox VE itself, using the cloud-init payload rendered by
``pve_cloudinit_payload.py``.

The endpoint does the parts that the Proxmox HTTP API supports natively
(image download via ``storage/{name}/download-url`` and VM create with
``--cicustom`` wiring) and returns the rendered snippet content plus
operator-side instructions so the snippets can be deposited into
``/var/lib/vz/snippets/`` on the host. Multipart snippet upload via the
Proxmox storage upload action is intentionally out of scope here — that
is a follow-up once ``proxmox-sdk`` grows native multipart support.
"""

from __future__ import annotations

import time
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.routes.cloud.provision import _extract_task_id, _wait_for_upid
from proxbox_api.routes.cloud.pve_cloudinit_payload import render_all
from proxbox_api.routes.proxmox_actions import _gate, _open_proxmox_session
from proxbox_api.schemas.cloud_provision import (
    PVETemplateBuildRequest,
    PVETemplateBuildResponse,
)
from proxbox_api.session.proxmox import ProxmoxSession
from proxbox_api.ssrf import validate_endpoint_url
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

router = APIRouter()

_IMAGE_EXTENSIONS = {".qcow2", ".raw", ".vmdk", ".vma"}


def _filename_from_request(req: PVETemplateBuildRequest) -> str:
    raw = (req.image_filename or "").strip()
    if not raw:
        raw = PurePosixPath(urlsplit(req.debian_image_url).path).name
    if not raw:
        raise HTTPException(status_code=422, detail="image_filename could not be derived.")
    path = PurePosixPath(raw)
    suffix = path.suffix.lower()
    if suffix == ".img":
        return f"{path.stem}.qcow2"
    if suffix not in _IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=f"image_filename must end with one of {sorted(_IMAGE_EXTENSIONS)}.",
        )
    return path.name


def _snippet_paths(req: PVETemplateBuildRequest) -> tuple[str, str, str, str]:
    """Compute snippet ``volid`` strings and the ``--cicustom`` argument.

    Returned tuple is ``(user_data, network_config, meta_data, cicustom)``.
    """
    prefix = f"pve-{req.vmid}"
    user_path = f"{req.snippets_storage}:snippets/{prefix}-user-data"
    network_path = f"{req.snippets_storage}:snippets/{prefix}-network-config.yaml"
    meta_path = f"{req.snippets_storage}:snippets/{prefix}-meta-data"
    cicustom = f"user={user_path},network={network_path},meta={meta_path}"
    return user_path, network_path, meta_path, cicustom


def _operator_instructions(
    *,
    target_node: str,
    snippet_user: str,
    snippet_network: str,
    snippet_meta: str,
) -> str:
    """Build the ssh + tee block the operator runs on the PVE host."""
    return (
        "# On the Proxmox VE host, drop the rendered snippets into the\n"
        "# snippets storage (default '/var/lib/vz/snippets/'). The API\n"
        "# returns the file contents inline; persist them with e.g.:\n"
        f"#   ssh root@{target_node}\n"
        f"#   cat > /var/lib/vz/snippets/{PurePosixPath(snippet_user.split(':snippets/', 1)[1]).name} <<'CIEOF'\n"
        "#   <paste user_data here>\n"
        "#   CIEOF\n"
        f"#   cat > /var/lib/vz/snippets/{PurePosixPath(snippet_network.split(':snippets/', 1)[1]).name} <<'CIEOF'\n"
        "#   <paste network_config here>\n"
        "#   CIEOF\n"
        f"#   cat > /var/lib/vz/snippets/{PurePosixPath(snippet_meta.split(':snippets/', 1)[1]).name} <<'CIEOF'\n"
        "#   <paste meta_data here>\n"
        "#   CIEOF\n"
    )


async def _vm_config_or_none(
    proxmox: ProxmoxSession,
    *,
    node: str,
    vmid: int,
) -> dict[str, Any] | None:
    try:
        config = await _maybe_await(proxmox.session.nodes(node).qemu(vmid).config.get())
    except Exception:  # noqa: BLE001
        return None
    return config if isinstance(config, dict) else {}


async def _image_exists(
    proxmox: ProxmoxSession,
    *,
    node: str,
    storage: str,
    volid: str,
) -> bool:
    try:
        content = await _maybe_await(
            proxmox.session.nodes(node).storage(storage).content.get(content="import")
        )
    except Exception:  # noqa: BLE001
        return False
    rows = content if isinstance(content, list) else []
    return any(isinstance(row, dict) and row.get("volid") == volid for row in rows)


async def _wait_for_task(
    proxmox: ProxmoxSession,
    *,
    node: str,
    response: object,
) -> str | None:
    upid = _extract_task_id(response)
    if upid and upid.startswith("UPID:"):
        await _wait_for_upid(proxmox, node, upid)
    return upid


@router.post(
    "/templates/pve",
    response_model=PVETemplateBuildResponse,
    status_code=status.HTTP_201_CREATED,
)
async def build_pve_template(
    req: PVETemplateBuildRequest,
    session: SessionDep,
) -> PVETemplateBuildResponse | JSONResponse:
    """Render PVE-installer cloud-init payloads and (optionally) create the VM.

    With ``create_vm=true`` (default) the endpoint will:

    1. Validate ``debian_image_url`` against the SSRF allowlist.
    2. Resolve the target ``ProxmoxEndpoint`` and gate it through the same
       ``allow_writes`` check used by the VM verb routes.
    3. Trigger a ``storage/{name}/download-url`` server-side fetch of the
       Debian image into ``image_storage``.
    4. Create the template VM with ``--cicustom`` pointing at the snippet
       paths it expects the operator to populate (see
       ``operator_instructions`` in the response).

    With ``create_vm=false`` the endpoint returns the rendered payloads and
    expected snippet paths without touching Proxmox — useful for a dry run
    from the NMS UI before the operator commits.
    """
    instance_id = f"{req.hostname}-{int(time.time())}"
    payloads = render_all(
        hostname=req.hostname,
        domain=req.domain,
        nic_name=req.nic_name,
        pve_version_pin=req.pve_version_pin,
        debian_release=req.debian_release,
        node_cidr=req.node_cidr,
        gateway=req.gateway,
        nameservers=tuple(req.nameservers),
        instance_id=instance_id,
        ssh_authorized_keys=req.ssh_authorized_keys,
    )

    user_path, network_path, meta_path, cicustom = _snippet_paths(req)
    operator_instructions = _operator_instructions(
        target_node=req.target_node,
        snippet_user=user_path,
        snippet_network=network_path,
        snippet_meta=meta_path,
    )

    filename = _filename_from_request(req)
    image_volid = f"{req.image_storage}:import/{filename}"

    if not req.create_vm:
        return PVETemplateBuildResponse(
            endpoint_id=req.endpoint_id,
            target_node=req.target_node,
            vmid=req.vmid,
            name=req.name,
            status="rendered",
            image_volid=image_volid,
            snippet_user_data_path=user_path,
            snippet_network_config_path=network_path,
            snippet_meta_data_path=meta_path,
            user_data=payloads.user_data,
            network_config=payloads.network_config,
            meta_data=payloads.meta_data,
            qm_cicustom=cicustom,
            operator_instructions=operator_instructions,
        )

    gated = await _gate(session, req.endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated

    safe, reason = validate_endpoint_url(req.debian_image_url)
    if not safe:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"debian_image_url rejected by SSRF protection: {reason}",
        )

    proxmox: ProxmoxSession | None = None
    try:
        proxmox = await _open_proxmox_session(gated)
        existing = await _vm_config_or_none(
            proxmox,
            node=req.target_node,
            vmid=req.vmid,
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"VMID {req.vmid} already exists on node {req.target_node}. "
                    "Choose a different VMID or delete the existing VM first."
                ),
            )

        download_upid = None
        if not await _image_exists(
            proxmox,
            node=req.target_node,
            storage=req.image_storage,
            volid=image_volid,
        ):
            download_result = await _maybe_await(
                proxmox.session.nodes(req.target_node)
                .storage(f"{req.image_storage}/download-url")
                .post(
                    content="import",
                    filename=filename,
                    url=req.debian_image_url,
                    **{"verify-certificates": 1 if req.verify_image_certificates else 0},
                )
            )
            download_upid = await _wait_for_task(
                proxmox,
                node=req.target_node,
                response=download_result,
            )

        create_kwargs: dict[str, object] = {
            "vmid": req.vmid,
            "name": req.name,
            "memory": req.memory_mb,
            "cores": req.cores,
            "sockets": 1,
            "ostype": "l26",
            "agent": "enabled=1",
            "scsihw": "virtio-scsi-pci",
            "scsi0": f"{req.vm_storage}:0,import-from={image_volid},discard=on",
            "ide2": f"{req.vm_storage}:cloudinit",
            "boot": "order=scsi0",
            "serial0": "socket",
            "vga": "serial0",
            "net0": f"virtio,bridge={req.bridge}",
            "cicustom": cicustom,
        }
        create_result = await _maybe_await(
            proxmox.session.nodes(req.target_node).qemu.post(**create_kwargs)
        )
        create_upid = await _wait_for_task(proxmox, node=req.target_node, response=create_result)

        return PVETemplateBuildResponse(
            endpoint_id=req.endpoint_id,
            target_node=req.target_node,
            vmid=req.vmid,
            name=req.name,
            status="created",
            image_volid=image_volid,
            snippet_user_data_path=user_path,
            snippet_network_config_path=network_path,
            snippet_meta_data_path=meta_path,
            user_data=payloads.user_data,
            network_config=payloads.network_config,
            meta_data=payloads.meta_data,
            qm_cicustom=cicustom,
            operator_instructions=operator_instructions,
            download_upid=download_upid,
            create_upid=create_upid,
        )
    finally:
        if proxmox is not None:
            await proxmox.aclose()

"""Cloud Portal VM provisioning route."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Awaitable, TypeVar

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import JSONResponse

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.exception import ProxboxException, ProxmoxAPIError
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_list_async
from proxbox_api.routes.intent.cloud_init import build_proxmox_ci_args
from proxbox_api.routes.intent.dispatchers.common import mapping_from_response
from proxbox_api.routes.proxmox_actions import _gate, _open_proxmox_session
from proxbox_api.schemas.cloud_provision import (
    CloudVMProvisionRequest,
    CloudVMProvisionResponse,
)
from proxbox_api.services.cloud_network import (
    AllocatedIPAddress,
    CloudNetworkConfig,
    allocate_ip,
    release_ip,
    resolve_cloud_network,
    validate_cloud_network_configured,
)
from proxbox_api.services.proxmox_helpers import get_node_task_status
from proxbox_api.services.sync.vm_network import ensure_ip_assigned_to_vm
from proxbox_api.services.verb_dispatch import (
    resolve_netbox_vm_id,
    resolve_proxmox_node,
    utcnow_iso,
    write_verb_journal_entry,
)
from proxbox_api.session.netbox import get_netbox_async_session
from proxbox_api.session.proxmox import ProxmoxSession
from proxbox_api.utils.async_compat import maybe_await as _maybe_await
from proxbox_api.utils.log_scrubbing import scrub_cloud_init

router = APIRouter()

_TASK_TIMEOUT_SECONDS = 300.0
_TASK_POLL_INTERVAL_SECONDS = 1.0
_JOURNAL_TIMEOUT_SECONDS = 5.0
_DRIVE_PREFIXES = ("ide", "sata", "scsi", "virtio")
_NETBOX_ENDPOINT_PATH = "/api/plugins/proxbox/endpoints/proxmox/"
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class _CloudNetworkLease:
    ip_id: int | None
    netbox_session: object


def _extract_task_id(response: object) -> str | None:
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        data = response.get("data")
        if isinstance(data, str):
            return data
        if isinstance(data, Mapping):
            task_id = data.get("upid") or data.get("UPID")
            return str(task_id) if task_id is not None else None
    return None


def _json_response_reason(response: JSONResponse) -> str:
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except (AttributeError, TypeError, ValueError, UnicodeDecodeError):
        return ""
    return str(payload.get("reason") or "") if isinstance(payload, dict) else ""


def _row_field(row: object, key: str) -> object:
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key, None)


def _row_ip_address(row: object) -> str:
    value = _row_field(row, "ip_address")
    if isinstance(value, Mapping):
        value = value.get("address")
    elif value is not None and not isinstance(value, str):
        value = getattr(value, "address", value)
    return str(value or "").split("/", 1)[0].strip()


def _endpoint_base_name(value: object) -> str:
    return re.sub(r"\s*\(nb:\d+\)\s*$", "", str(value or "").strip())


def _netbox_row_matches_endpoint(row: object, endpoint: ProxmoxEndpoint) -> bool:
    row_name = _endpoint_base_name(_row_field(row, "name"))
    endpoint_name = _endpoint_base_name(endpoint.name)
    if row_name and endpoint_name and row_name == endpoint_name:
        return True

    row_domain = str(_row_field(row, "domain") or "").strip()
    if row_domain and endpoint.domain and row_domain == endpoint.domain:
        return True

    return bool(
        _row_ip_address(row) and _row_ip_address(row) == endpoint.ip_address.split("/", 1)[0]
    )


async def _netbox_allows_endpoint_writes(session: object, endpoint: ProxmoxEndpoint) -> bool:
    """Return whether the matching NetBox Proxbox endpoint currently allows writes."""
    try:
        nb = await get_netbox_async_session(database_session=session)
        rows = await rest_list_async(nb, _NETBOX_ENDPOINT_PATH, query={"limit": 0})
    except Exception as error:  # noqa: BLE001
        logger.info(
            "cloud provision: unable to confirm NetBox allow_writes for endpoint id=%s: %s",
            endpoint.id,
            error,
        )
        return False

    matched_rows = [row for row in rows if _netbox_row_matches_endpoint(row, endpoint)]
    return any(bool(_row_field(row, "allow_writes")) for row in matched_rows)


async def _cloud_provision_gate(
    session: object,
    endpoint_id: int | None,
) -> JSONResponse | ProxmoxEndpoint:
    """Gate cloud VM provisioning while honoring current NetBox allow_writes state.

    Local proxbox-api endpoint rows can lag behind the NetBox plugin endpoint
    toggle used by NMS. When the only gate failure is local stale
    ``allow_writes=False``, a current matching NetBox endpoint with
    ``allow_writes=True`` is authoritative for Cloud VM provisioning.
    """
    gated = await _gate(session, endpoint_id)
    if not isinstance(gated, JSONResponse):
        return gated
    if gated.status_code != status.HTTP_403_FORBIDDEN:
        return gated
    if _json_response_reason(gated) not in {
        "endpoint_writes_disabled",
        "writes_disabled_for_endpoint",
    }:
        return gated
    if endpoint_id is None:
        return gated

    endpoint = await _maybe_await(session.get(ProxmoxEndpoint, endpoint_id))
    if endpoint is None:
        return gated
    if not await _netbox_allows_endpoint_writes(session, endpoint):
        return gated

    endpoint.allow_writes = True
    session.add(endpoint)
    await _maybe_await(session.commit())
    await _maybe_await(session.refresh(endpoint))
    logger.info(
        "cloud provision: refreshed allow_writes from NetBox for ProxmoxEndpoint id=%s",
        endpoint_id,
    )
    return endpoint


def _is_upid(value: str | None) -> bool:
    return bool(value and value.startswith("UPID:"))


def _should_wait_for_upid() -> bool:
    return os.getenv("PROXMOX_API_MODE") != "mock"


def _is_mock_proxmox_mode() -> bool:
    return os.getenv("PROXMOX_API_MODE") == "mock"


async def _wait_for_upid(proxmox: ProxmoxSession, node: str, upid: str) -> None:
    async def _poll() -> None:
        while True:
            task_status = await get_node_task_status(proxmox, node, upid)
            status_value = getattr(task_status, "status", None)
            exitstatus = getattr(task_status, "exitstatus", None)
            if status_value == "stopped":
                if exitstatus in (None, "OK"):
                    return
                raise ProxmoxAPIError(message=f"Proxmox task failed with exitstatus={exitstatus}")
            await asyncio.sleep(_TASK_POLL_INTERVAL_SECONDS)

    await asyncio.wait_for(_poll(), timeout=_TASK_TIMEOUT_SECONDS)


def _has_cloudinit_drive(config_payload: object) -> bool:
    config = mapping_from_response(config_payload)
    for key, value in config.items():
        if not any(key.startswith(prefix) for prefix in _DRIVE_PREFIXES):
            continue
        if "cloudinit" in str(value).lower():
            return True
    return False


def _pick_unused_ide_slot(config_payload: object | None) -> str | None:
    if config_payload is None:
        return "ide2"
    config = mapping_from_response(config_payload)
    for slot in ("ide2", "ide0", "ide1", "ide3"):
        if slot not in config:
            return slot
    return None


def _infer_default_storage(config_payload: object | None) -> str:
    if config_payload is not None:
        for value in mapping_from_response(config_payload).values():
            if not isinstance(value, str) or ":" not in value:
                continue
            storage, _sep, _volume = value.partition(":")
            if storage and "cloudinit" not in value.lower():
                return storage
    return "local-lvm"


def _build_net0_override(
    existing_config: object | None,
    *,
    bridge: str | None,
    vlan_tag: int | None,
) -> str | None:
    """Return a net0 config that preserves model/MAC while overriding bridge/tag."""
    if bridge is None and vlan_tag is None:
        return None

    config = mapping_from_response(existing_config)
    existing_net0 = str(config.get("net0") or "virtio")
    parts = [part.strip() for part in existing_net0.split(",") if part.strip()]
    if not parts:
        parts = ["virtio"]

    rewritten: list[str] = []
    saw_bridge = False
    saw_tag = False
    for part in parts:
        if part.startswith("bridge="):
            saw_bridge = True
            if bridge is not None:
                rewritten.append(f"bridge={bridge}")
            else:
                rewritten.append(part)
            continue
        if part.startswith("tag="):
            saw_tag = True
            if vlan_tag is not None:
                rewritten.append(f"tag={vlan_tag}")
            continue
        rewritten.append(part)

    if bridge is not None and not saw_bridge:
        rewritten.append(f"bridge={bridge}")
    if vlan_tag is not None and not saw_tag:
        rewritten.append(f"tag={vlan_tag}")

    return ",".join(rewritten)


def _proxmox_step_failed(step: str, error: Exception) -> HTTPException:
    safe_error = scrub_cloud_init({"error": str(error)}).get("error", str(error))
    logger.warning("cloud provision failed at step=%s: %s", step, safe_error)
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "reason": "proxmox_step_failed",
            "step": step,
            "error": safe_error,
        },
    )


def _cloud_network_http_exception(error: ValueError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=str(error),
    )


def _request_with_cloud_network(
    req: CloudVMProvisionRequest,
    cloud_network: CloudNetworkConfig,
    allocated_ip: AllocatedIPAddress,
) -> CloudVMProvisionRequest:
    cloud_init = req.cloud_init.model_copy(
        update={"network": {"ip": allocated_ip.cidr, "gw": cloud_network.gateway}}
    )
    return req.model_copy(
        update={
            "bridge": cloud_network.bridge,
            "vlan_tag": cloud_network.vlan_tag,
            "cloud_init": cloud_init,
        }
    )


def _require_resolved_cloud_network() -> CloudNetworkConfig:
    cloud_network = resolve_cloud_network()
    try:
        validate_cloud_network_configured(cloud_network)
    except ValueError as error:
        raise _cloud_network_http_exception(error) from error
    return cloud_network


async def _prepare_qemu_cloud_network_request(
    req: CloudVMProvisionRequest,
    session: object,
) -> tuple[CloudVMProvisionRequest, _CloudNetworkLease | None]:
    if not req.enforce_cloud_network:
        return req, None

    cloud_network = _require_resolved_cloud_network()
    if cloud_network.prefix_id is None:
        raise _cloud_network_http_exception(ValueError("cloud network not configured"))
    nb = await get_netbox_async_session(database_session=session)
    allocated_ip = await allocate_ip(cloud_network.prefix_id, netbox_session=nb)
    return _request_with_cloud_network(req, cloud_network, allocated_ip), _CloudNetworkLease(
        ip_id=allocated_ip.id,
        netbox_session=nb,
    )


async def _release_cloud_network_lease(lease: _CloudNetworkLease | None) -> None:
    if lease is None or lease.ip_id is None:
        return
    await release_ip(lease.ip_id, netbox_session=lease.netbox_session)


async def _run_proxmox_step_with_cloud_network_rollback(
    step: str,
    operation: Awaitable[_T],
    lease: _CloudNetworkLease | None,
) -> _T:
    if lease is None:
        # No cloud-network allocation to roll back: preserve legacy behavior
        # exactly (the original exception propagates unchanged, same status/body)
        # so enforce_cloud_network=False stays byte-identical to pre-feature.
        return await operation
    try:
        return await operation
    except Exception as error:  # noqa: BLE001
        # NetBox allocation happens before Proxmox writes so the guest can
        # receive a stable CIDR; release it if any later Proxmox step fails.
        await _release_cloud_network_lease(lease)
        raise _proxmox_step_failed(step, error) from error


async def _bind_allocated_ip_to_vm_best_effort(
    *,
    req: CloudVMProvisionRequest,
    lease: _CloudNetworkLease | None,
) -> None:
    if lease is None or lease.ip_id is None:
        return
    try:
        netbox_vm_id = await resolve_netbox_vm_id(lease.netbox_session, req.new_vmid)
        if netbox_vm_id is None:
            logger.info(
                "cloud provision: allocated IP id=%s for vmid=%s remains unbound; "
                "NetBox VM not found yet",
                lease.ip_id,
                req.new_vmid,
            )
            return
        assigned, reason = await ensure_ip_assigned_to_vm(
            lease.netbox_session,
            lease.ip_id,
            netbox_vm_id,
        )
        if assigned:
            logger.info(
                "cloud provision: bound allocated IP id=%s to NetBox VM id=%s (%s)",
                lease.ip_id,
                netbox_vm_id,
                reason,
            )
        else:
            logger.info(
                "cloud provision: allocated IP id=%s not bound to NetBox VM id=%s (%s)",
                lease.ip_id,
                netbox_vm_id,
                reason,
            )
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "cloud provision: failed to bind allocated IP id=%s for vmid=%s: %s",
            lease.ip_id,
            req.new_vmid,
            error,
        )


async def _get_qemu_config_best_effort(
    proxmox: ProxmoxSession,
    *,
    node: str,
    vmid: int,
) -> object | None:
    try:
        return await _maybe_await(proxmox.session.nodes(node).qemu(vmid).config.get())
    except Exception as error:  # noqa: BLE001
        logger.info(
            "cloud provision could not read qemu config for vmid=%s node=%s; adding cloudinit drive defensively: %s",
            vmid,
            node,
            error,
        )
        return None


async def _journal_provision(
    *,
    session: object,
    proxmox: ProxmoxSession,
    endpoint: object,
    request: CloudVMProvisionRequest,
    response: CloudVMProvisionResponse,
    actor: str | None,
) -> None:
    try:
        nb = await get_netbox_async_session(database_session=session)
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "cloud provision: NetBox session unavailable while journaling vmid=%s: %s",
            request.new_vmid,
            scrub_cloud_init({"error": str(error)}).get("error", str(error)),
        )
        return

    try:
        netbox_vm_id = await resolve_netbox_vm_id(nb, request.new_vmid)
    except ProxboxException as error:
        logger.warning(
            "cloud provision: failed to resolve NetBox VM for vmid=%s: %s",
            request.new_vmid,
            error,
        )
        return
    if netbox_vm_id is None:
        logger.info(
            "cloud provision: no NetBox VM found for vmid=%s; skipping journal entry",
            request.new_vmid,
        )
        return

    resolved_node = await resolve_proxmox_node(proxmox, "qemu", request.new_vmid)
    node = resolved_node if isinstance(resolved_node, str) else request.target_node
    comments = "\n".join(
        [
            "Proxbox operational verb dispatched.",
            "",
            "- verb: cloud_init_provision",
            f"- actor: {actor or 'proxbox-api'}",
            f"- result: {response.status}",
            f"- endpoint: {getattr(endpoint, 'name', 'unknown')} (id={getattr(endpoint, 'id', 0)})",
            f"- dispatched_at: {utcnow_iso()}",
            f"- target_vmid: {request.new_vmid}",
            f"- template_vmid: {request.template_vmid}",
            f"- target_node: {node}",
            f"- clone_upid: {response.clone_upid or ''}",
            f"- config_upid: {response.config_upid or ''}",
            f"- start_upid: {response.start_upid or ''}",
        ]
    )
    await write_verb_journal_entry(
        nb,
        netbox_vm_id=netbox_vm_id,
        kind="success" if response.status == "started" else "info",
        comments=comments,
    )


async def _journal_provision_best_effort(
    *,
    session: object,
    proxmox: ProxmoxSession,
    endpoint: object,
    request: CloudVMProvisionRequest,
    response: CloudVMProvisionResponse,
    actor: str | None,
) -> None:
    if _is_mock_proxmox_mode():
        return
    try:
        await asyncio.wait_for(
            _journal_provision(
                session=session,
                proxmox=proxmox,
                endpoint=endpoint,
                request=request,
                response=response,
                actor=actor,
            ),
            timeout=_JOURNAL_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        logger.warning(
            "cloud provision: journal write timed out after %.1fs for vmid=%s",
            _JOURNAL_TIMEOUT_SECONDS,
            request.new_vmid,
        )
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "cloud provision: journal write failed for vmid=%s: %s",
            request.new_vmid,
            scrub_cloud_init({"error": str(error)}).get("error", str(error)),
        )


async def _clone_template_vm(proxmox: ProxmoxSession, req: CloudVMProvisionRequest) -> str | None:
    template_node = await resolve_proxmox_node(proxmox, "qemu", req.template_vmid)
    if not isinstance(template_node, str) or not template_node:
        try:
            await _maybe_await(
                proxmox.session.nodes(req.target_node).qemu(req.template_vmid).config.get()
            )
        except Exception as error:  # noqa: BLE001
            logger.info(
                "cloud provision could not resolve template vmid=%s via cluster/resources "
                "or target_node=%s: %s",
                req.template_vmid,
                req.target_node,
                error,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "reason": "template_vmid_not_found",
                    "template_vmid": req.template_vmid,
                },
            ) from error
        template_node = req.target_node

    clone_payload: dict[str, object] = {
        "newid": req.new_vmid,
        "name": req.new_name,
        "target": req.target_node,
        "full": 1 if req.full_clone else 0,
    }
    if req.storage:
        clone_payload["storage"] = req.storage
    clone_result = await _maybe_await(
        proxmox.session.nodes(template_node).qemu(req.template_vmid).clone.post(**clone_payload)
    )
    clone_upid = _extract_task_id(clone_result)
    if _is_upid(clone_upid) and _should_wait_for_upid():
        await _wait_for_upid(proxmox, template_node, clone_upid)
    return clone_upid


async def _configure_cloud_init_vm(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
) -> str | None:
    existing_config = await _get_qemu_config_best_effort(
        proxmox,
        node=req.target_node,
        vmid=req.new_vmid,
    )
    ci_args = build_proxmox_ci_args(req.cloud_init)
    if req.memory_mb is not None:
        ci_args["memory"] = req.memory_mb
    if req.cores is not None:
        ci_args["cores"] = req.cores
    if req.sockets is not None:
        ci_args["sockets"] = req.sockets
    net0 = _build_net0_override(existing_config, bridge=req.bridge, vlan_tag=req.vlan_tag)
    if net0 is not None:
        ci_args["net0"] = net0
    if req.enable_agent:
        # Force the QEMU guest agent on regardless of what the source template
        # carried, so every cloud clone reports guest IPs and shuts down
        # gracefully (templates bake qemu-guest-agent via netbox-packer).
        ci_args["agent"] = "enabled=1"
    if existing_config is None or not _has_cloudinit_drive(existing_config):
        slot = _pick_unused_ide_slot(existing_config)
        if slot is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "reason": "no_unused_ide_slot",
                    "vmid": req.new_vmid,
                },
            )
        storage = req.storage or _infer_default_storage(existing_config)
        ci_args[slot] = f"{storage}:cloudinit"

    config_result = await _maybe_await(
        proxmox.session.nodes(req.target_node).qemu(req.new_vmid).config.put(**ci_args)
    )
    return _extract_task_id(config_result)


async def _resize_vm_disk(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
) -> str | None:
    if req.disk_gb is None:
        return None
    resize_result = await _maybe_await(
        proxmox.session.nodes(req.target_node)
        .qemu(req.new_vmid)
        .resize.put(disk="scsi0", size=f"{req.disk_gb}G")
    )
    return _extract_task_id(resize_result)


async def _start_vm_after_provision(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
) -> str | None:
    if not req.start_after_provision:
        return None
    start_result = await _maybe_await(
        proxmox.session.nodes(req.target_node).qemu(req.new_vmid).status.start.post()
    )
    return _extract_task_id(start_result)


@router.post("/vm/provision", response_model=CloudVMProvisionResponse)
async def provision_vm(
    req: CloudVMProvisionRequest,
    session: SessionDep,
    actor: Annotated[str | None, Header(alias="X-Proxbox-Actor")] = None,
) -> CloudVMProvisionResponse | JSONResponse:
    gated = await _cloud_provision_gate(session, req.endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated

    proxmox: ProxmoxSession | None = None
    lease: _CloudNetworkLease | None = None
    try:
        req, lease = await _prepare_qemu_cloud_network_request(req, session)
        proxmox = await _run_proxmox_step_with_cloud_network_rollback(
            "open_session",
            _open_proxmox_session(gated),
            lease,
        )
        clone_upid = await _run_proxmox_step_with_cloud_network_rollback(
            "clone",
            _clone_template_vm(proxmox, req),
            lease,
        )
        config_upid = await _run_proxmox_step_with_cloud_network_rollback(
            "configure_cloud_init",
            _configure_cloud_init_vm(proxmox, req),
            lease,
        )
        if _is_upid(config_upid) and _should_wait_for_upid():
            await _run_proxmox_step_with_cloud_network_rollback(
                "configure_cloud_init",
                _wait_for_upid(proxmox, req.target_node, config_upid),
                lease,
            )

        resize_upid = await _run_proxmox_step_with_cloud_network_rollback(
            "resize_disk",
            _resize_vm_disk(proxmox, req),
            lease,
        )
        if _is_upid(resize_upid) and _should_wait_for_upid():
            await _run_proxmox_step_with_cloud_network_rollback(
                "resize_disk",
                _wait_for_upid(proxmox, req.target_node, resize_upid),
                lease,
            )

        start_upid = await _run_proxmox_step_with_cloud_network_rollback(
            "start",
            _start_vm_after_provision(proxmox, req),
            lease,
        )

        response = CloudVMProvisionResponse(
            new_vmid=req.new_vmid,
            clone_upid=clone_upid,
            config_upid=config_upid,
            resize_upid=resize_upid,
            start_upid=start_upid,
            status="started" if req.start_after_provision else "stopped",
        )
        await _journal_provision_best_effort(
            session=session,
            proxmox=proxmox,
            endpoint=gated,
            request=req,
            response=response,
            actor=actor,
        )
        await _bind_allocated_ip_to_vm_best_effort(req=req, lease=lease)
        return response
    except HTTPException:
        raise
    except Exception:
        await _release_cloud_network_lease(lease)
        raise
    finally:
        if proxmox is not None:
            await proxmox.aclose()

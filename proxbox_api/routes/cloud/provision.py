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
from proxbox_api.services.proxmox_helpers import get_cluster_resources, get_node_task_status
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
_DISK_CONFIG_KEY = re.compile(r"(?P<bus>scsi|virtio|sata|ide|efidisk|tpmstate)(?P<index>\d+)\Z")
_DISK_BUS_ORDER = {
    "scsi": 0,
    "virtio": 1,
    "sata": 2,
    "ide": 3,
    "efidisk": 4,
    "tpmstate": 5,
}
_EXTRA_DISK_STORAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_NETBOX_ENDPOINT_PATH = "/api/plugins/proxbox/endpoints/proxmox/"
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class _CloudNetworkLease:
    ip_id: int | None
    netbox_session: object


@dataclass(frozen=True, slots=True)
class _ClonePlan:
    template_node: str
    needs_relocation: bool


@dataclass(frozen=True, slots=True)
class _CloneTemplateResult:
    clone_upid: str | None
    relocate_upids: list[str]


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
    toggle used by control plane. When the only gate failure is local stale
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


def _disk_entry_storage(key: str, value: object) -> tuple[int, int, int, str] | None:
    match = _DISK_CONFIG_KEY.fullmatch(key)
    if match is None or not isinstance(value, str):
        return None
    volume_ref, *options = value.strip().split(",")
    storage, separator, volume = volume_ref.partition(":")
    if not separator or not _EXTRA_DISK_STORAGE_ID.fullmatch(storage) or not volume:
        return None
    if "cloudinit" in volume.lower():
        return None
    if any(option.strip().lower() == "media=cdrom" for option in options):
        return None
    bus = match.group("bus")
    index = int(match.group("index"))
    return (0 if key == "scsi0" else 1, index, _DISK_BUS_ORDER[bus], storage)


def _infer_default_storage(config_payload: object | None) -> str:
    candidates = [
        candidate
        for key, value in mapping_from_response(config_payload).items()
        if (candidate := _disk_entry_storage(key, value)) is not None
    ]
    return min(candidates)[-1] if candidates else "local-lvm"


def _config_mapping_for_extra_disks(config_payload: object) -> dict[str, object] | None:
    value = config_payload
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            value = model_dump(mode="python", by_alias=True, exclude_none=True)
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(value, Mapping):
        return None
    if "data" in value:
        value = value.get("data")
        if not isinstance(value, Mapping):
            return None
    return {str(key): item for key, item in value.items()}


def _extra_disk_config_args(
    req: CloudVMProvisionRequest,
    existing_config: object | None,
) -> dict[str, object]:
    if not req.extra_disks:
        return {}
    config = _config_mapping_for_extra_disks(existing_config)
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "config_read_failed"},
        )
    args: dict[str, object] = build_extra_disk_args(
        req,
        config,
        _infer_default_storage(config),
    )
    digest = config.get("digest")
    if isinstance(digest, str) and digest:
        args["digest"] = digest
    return args


def build_extra_disk_args(
    req: CloudVMProvisionRequest,
    existing_config: object | None,
    default_storage: str,
) -> dict[str, str]:
    """Build validated extra-disk config arguments without changing Proxmox."""

    existing = mapping_from_response(existing_config)
    disk_args: dict[str, str] = {}
    for disk in req.extra_disks:
        slot = f"{disk.bus}{disk.index}"
        if slot in existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"reason": "extra_disk_slot_in_use", "slot": slot},
            )
        storage = disk.storage or req.storage or default_storage
        if not _EXTRA_DISK_STORAGE_ID.fullmatch(storage):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"reason": "extra_disk_storage_invalid", "slot": slot},
            )
        value = f"{storage}:{disk.size_gb}"
        if disk.discard:
            value += ",discard=on"
        if disk.ssd:
            value += ",ssd=1"
        disk_args[slot] = value
    return disk_args


def _build_vm_metadata_args(req: CloudVMProvisionRequest) -> dict[str, str]:
    metadata: dict[str, str] = {}
    if req.tags:
        metadata["tags"] = ";".join(tag.lower() for tag in req.tags)
    if req.description is not None:
        metadata["description"] = req.description
    return metadata


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


def _existing_network_mac(existing_config: object | None, key: str) -> str | None:
    value = mapping_from_response(existing_config).get(key)
    if not isinstance(value, str):
        return None
    for part in (part.strip() for part in value.split(",")):
        name, separator, mac = part.partition("=")
        if separator and mac and (name == "macaddr" or _looks_like_mac(mac)):
            return mac
    return None


def _looks_like_mac(value: str) -> bool:
    return re.fullmatch(r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}", value) is not None


def _build_extra_network_args(
    req: CloudVMProvisionRequest,
    existing_config: object | None,
) -> dict[str, str]:
    if req.extra_networks and _config_mapping_for_extra_disks(existing_config) is None:
        # Without a readable config an existing NIC's MAC cannot be preserved, so
        # Proxmox would silently mint a new one; refuse like extra disks do.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "config_read_failed"},
        )
    network_args: dict[str, str] = {}
    for network in req.extra_networks:
        key = f"net{network.index}"
        mac = _existing_network_mac(existing_config, key)
        value = network.model + (f"={mac}" if mac else "")
        value += f",bridge={network.bridge}"
        if network.vlan_tag is not None:
            value += f",tag={network.vlan_tag}"
        if network.firewall is not None:
            value += f",firewall={int(network.firewall)}"
        network_args[key] = value
    return network_args


def _reject_managed_extra_networks(req: CloudVMProvisionRequest) -> None:
    if req.enforce_cloud_network and req.extra_networks:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "extra_networks_not_allowed_with_cloud_network"},
        )


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


_PROVISION_REFUSAL_REASONS = frozenset(
    {
        "config_read_failed",
        "extra_disk_slot_in_use",
        "extra_disk_storage_invalid",
        "relocate_disk_failed",
        "storage_lookup_failed",
        "target_storage_unavailable",
    }
)


def _is_provision_refusal(error: Exception) -> bool:
    """Return whether the error is a stable provisioning refusal clients must see."""

    return (
        isinstance(error, HTTPException)
        and isinstance(error.detail, dict)
        and error.detail.get("reason") in _PROVISION_REFUSAL_REASONS
    )


async def _run_proxmox_step_with_cloud_network_rollback(
    step: str,
    operation: Awaitable[_T],
    lease: _CloudNetworkLease | None,
) -> _T:
    try:
        return await operation
    except Exception as error:  # noqa: BLE001
        # NetBox allocation happens before Proxmox writes so the guest can
        # receive a stable CIDR; release it if any later Proxmox step fails.
        if lease is not None:
            await _release_cloud_network_lease(lease)
        if _is_provision_refusal(error):
            raise
        # Always route through _proxmox_step_failed so the cipassword is scrubbed
        # from the log + client body even on the default enforce_cloud_network=
        # False path — parity with the SSE stream, which scrubs unconditionally.
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


async def _resolve_template_node(proxmox: ProxmoxSession, req: CloudVMProvisionRequest) -> str:
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
        return req.target_node
    return template_node


async def _storage_supports_cross_node_clone(
    proxmox: ProxmoxSession,
    *,
    template_node: str,
    target_node: str,
    storage: str,
) -> bool:
    try:
        resources = await get_cluster_resources(proxmox, resource_type="storage")
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "storage_lookup_failed", "storage": storage},
        ) from error
    _require_target_storage(resources, storage=storage, target_node=target_node)
    return any(
        _row_field(row, "node") == template_node
        and _row_field(row, "storage") == storage
        and _row_field(row, "shared") == 1
        for row in resources
    )


def _require_target_storage(resources: list[object], *, storage: str, target_node: str) -> None:
    destination = next(
        (
            row
            for row in resources
            if _row_field(row, "node") == target_node and _row_field(row, "storage") == storage
        ),
        None,
    )
    destination_status = _row_field(destination, "status") if destination is not None else None
    if destination is None or (
        destination_status is not None and destination_status != "available"
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "reason": "target_storage_unavailable",
                "storage": storage,
                "target_node": target_node,
            },
        )


async def _plan_template_clone(proxmox: ProxmoxSession, req: CloudVMProvisionRequest) -> _ClonePlan:
    template_node = await _resolve_template_node(proxmox, req)
    if template_node == req.target_node or req.storage is None:
        return _ClonePlan(template_node=template_node, needs_relocation=False)
    usable = await _storage_supports_cross_node_clone(
        proxmox,
        template_node=template_node,
        target_node=req.target_node,
        storage=req.storage,
    )
    return _ClonePlan(template_node=template_node, needs_relocation=not usable)


async def _dispatch_template_clone(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
    plan: _ClonePlan,
) -> str | None:

    clone_payload: dict[str, object] = {
        "newid": req.new_vmid,
        "name": req.new_name,
        "target": req.target_node,
        "full": 1 if req.full_clone else 0,
    }
    if req.storage and not plan.needs_relocation:
        clone_payload["storage"] = req.storage
    clone_result = await _maybe_await(
        proxmox.session.nodes(plan.template_node)
        .qemu(req.template_vmid)
        .clone.post(**clone_payload)
    )
    clone_upid = _extract_task_id(clone_result)
    if _is_upid(clone_upid) and _should_wait_for_upid():
        await _wait_for_upid(proxmox, plan.template_node, clone_upid)
    return clone_upid


def _relocatable_disks(config_payload: object, storage: str) -> list[str]:
    disks: list[tuple[tuple[int, int, int, str], str]] = []
    for key, value in mapping_from_response(config_payload).items():
        candidate = _disk_entry_storage(key, value)
        if candidate is not None and candidate[-1] != storage:
            disks.append((candidate, key))
    return [key for _, key in sorted(disks)]


def _relocation_failure(
    req: CloudVMProvisionRequest,
    clone_upid: str | None,
    relocate_upids: dict[str, str],
    failed_disk: str | None,
    error: object,
) -> HTTPException:
    """Build the evidence-preserving relocation refusal for REST and SSE callers."""
    safe_error = scrub_cloud_init({"error": str(error)}).get("error", str(error))
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={
            "reason": "relocate_disk_failed",
            "vmid": req.new_vmid,
            "target_node": req.target_node,
            "clone_upid": clone_upid,
            "relocate_upids": relocate_upids,
            "failed_disk": failed_disk,
            "error": safe_error,
        },
    )


async def _read_relocation_config(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
    clone_upid: str | None,
) -> dict[str, object]:
    """Read the cloned VM config, failing closed when it is unusable.

    A degraded read (null, a list, an empty ``data`` envelope, or a config that
    lists no disk) would otherwise relocate nothing and let provisioning report
    success with the disks still on the template storage.
    """
    try:
        payload = await _maybe_await(
            proxmox.session.nodes(req.target_node).qemu(req.new_vmid).config.get()
        )
    except Exception as error:  # noqa: BLE001
        raise _relocation_failure(req, clone_upid, {}, None, error) from error
    config = _config_mapping_for_extra_disks(payload)
    has_disk = config is not None and any(
        _disk_entry_storage(key, value) is not None for key, value in config.items()
    )
    if config is None or not has_disk:
        raise _relocation_failure(
            req, clone_upid, {}, None, "cloned VM config is unreadable or lists no disk"
        )
    return config


async def _relocate_vm_disks(
    proxmox: ProxmoxSession,
    req: CloudVMProvisionRequest,
    plan: _ClonePlan,
    clone_upid: str | None,
) -> list[str]:
    if not plan.needs_relocation or req.storage is None:
        return []
    config = await _read_relocation_config(proxmox, req, clone_upid)
    relocate_upids: dict[str, str] = {}
    for disk in _relocatable_disks(config, req.storage):
        try:
            result = await _maybe_await(
                proxmox.session.nodes(req.target_node)
                .qemu(req.new_vmid)
                .move_disk.post(disk=disk, storage=req.storage, delete=0)
            )
            upid = _extract_task_id(result)
            if upid is not None:
                relocate_upids[disk] = upid
            if _is_upid(upid) and _should_wait_for_upid():
                await _wait_for_upid(proxmox, req.target_node, upid)
        except Exception as error:  # noqa: BLE001
            raise _relocation_failure(req, clone_upid, relocate_upids, disk, error) from error
    return list(relocate_upids.values())


async def _clone_template_vm(
    proxmox: ProxmoxSession, req: CloudVMProvisionRequest
) -> _CloneTemplateResult:
    plan = await _plan_template_clone(proxmox, req)
    clone_upid = await _dispatch_template_clone(proxmox, req, plan)
    relocate_upids = await _relocate_vm_disks(proxmox, req, plan, clone_upid)
    return _CloneTemplateResult(clone_upid=clone_upid, relocate_upids=relocate_upids)


def _build_agent_override(existing_config: dict | None) -> str:
    """Force the QEMU guest agent on while preserving any existing sub-options.

    A ``config.put(agent=...)`` replaces the whole field, so a source template
    carrying ``enabled=1,fstrim_cloned_disks=1`` would otherwise lose
    ``fstrim_cloned_disks`` on clone. Mirror ``_build_net0_override`` and merge:
    keep every non-``enabled`` sub-option and force a single ``enabled=1``.
    """
    # Normalize through mapping_from_response like the sibling helpers
    # (_build_net0_override, _has_cloudinit_drive) so a Pydantic model or a
    # {"data": ...} envelope is handled defensively instead of raising.
    existing = str(mapping_from_response(existing_config).get("agent") or "").strip()
    if not existing:
        return "enabled=1"
    merged: list[str] = []
    enabled_set = False
    for part in (p.strip() for p in existing.split(",") if p.strip()):
        if part in {"0", "1"} or part.startswith("enabled="):
            if not enabled_set:
                merged.append("enabled=1")
                enabled_set = True
        else:
            merged.append(part)
    if not enabled_set:
        merged.insert(0, "enabled=1")
    return ",".join(merged)


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
    ci_args.update(_build_extra_network_args(req, existing_config))
    if req.enable_agent:
        # Force the QEMU guest agent on regardless of what the source template
        # carried, so every cloud clone reports guest IPs and shuts down
        # gracefully (templates bake qemu-guest-agent via netbox-packer). Merge
        # rather than clobber so a template's agent sub-options survive.
        ci_args["agent"] = _build_agent_override(existing_config)
    ci_args.update(_extra_disk_config_args(req, existing_config))
    ci_args.update(_build_vm_metadata_args(req))
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
    _reject_managed_extra_networks(req)
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
        clone_plan = await _run_proxmox_step_with_cloud_network_rollback(
            "clone",
            _plan_template_clone(proxmox, req),
            lease,
        )
        clone_upid = await _run_proxmox_step_with_cloud_network_rollback(
            "clone",
            _dispatch_template_clone(proxmox, req, clone_plan),
            lease,
        )
        relocate_upids = await _run_proxmox_step_with_cloud_network_rollback(
            "relocate_disk",
            _relocate_vm_disks(proxmox, req, clone_plan, clone_upid),
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
            relocate_upids=relocate_upids,
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

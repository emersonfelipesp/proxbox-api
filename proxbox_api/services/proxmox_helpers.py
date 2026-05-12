"""Typed helpers for proxmox-sdk calls validated through generated models."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from proxmox_sdk.sdk.exceptions import (
    ProxmoxConnectionError,
    ProxmoxTimeoutError,
    ResourceException,
)

from proxbox_api.exception import ProxboxException, ProxmoxAPIError
from proxbox_api.generated.proxmox.latest import pydantic_models as generated_models
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.session.proxmox import ProxmoxSession


def _model_dump(model: object) -> dict[str, object]:
    return model.model_dump(mode="python", by_alias=True, exclude_none=True)


_T = TypeVar("_T")


def _dual_mode(async_fn: Callable[..., _T]) -> Callable[..., _T]:
    """Allow async helpers to be called from both async and sync contexts."""

    @functools.wraps(async_fn)
    def wrapper(*args: object, **kwargs: object) -> _T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(async_fn(*args, **kwargs))
        return async_fn(*args, **kwargs)

    return wrapper


@_dual_mode
async def get_cluster_status(
    session: ProxmoxSession,
) -> list[generated_models.GetClusterStatusResponseItem]:
    """Get cluster status from Proxmox."""
    try:
        result = await resolve_async(session.session("cluster/status").get())
        validated = generated_models.GetClusterStatusResponse.model_validate(result)
        return validated.root
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(
            message="Proxmox cluster status request timed out", original_error=error
        )
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for cluster status", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error fetching Proxmox cluster status",
            original_error=error,
        )


@_dual_mode
async def get_cluster_resources(
    session: ProxmoxSession,
    resource_type: str | None = None,
) -> list[generated_models.GetClusterResourcesResponseItem]:
    """Get cluster resources from Proxmox."""
    try:
        if resource_type:
            result = await resolve_async(
                session.session("cluster/resources").get(type=resource_type)
            )
        else:
            result = await resolve_async(session.session("cluster/resources").get())
        validated = generated_models.GetClusterResourcesResponse.model_validate(result)
        return validated.root
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(
            message="Proxmox cluster resources request timed out", original_error=error
        )
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for cluster resources", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error fetching Proxmox cluster resources",
            original_error=error,
        )


@_dual_mode
async def get_cluster_replication(
    session: ProxmoxSession,
) -> list[dict[str, object]]:
    """Get cluster replication jobs from Proxmox."""
    try:
        result = await resolve_async(session.session("cluster/replication").get())
        return result if isinstance(result, list) else []
    except Exception:
        return []


@_dual_mode
async def get_ha_status_current(
    session: ProxmoxSession,
) -> list[generated_models.GetClusterHaStatusCurrentResponseItem]:
    """Get current HA service/CRM status from Proxmox.

    Mirrors `GET /cluster/ha/status/current`. The list includes per-service
    rows (`type=service`) plus quorum/master/lrm rows that describe the
    cluster as a whole.
    """
    try:
        result = await resolve_async(session.session("cluster/ha/status/current").get())
        validated = generated_models.GetClusterHaStatusCurrentResponse.model_validate(result)
        return validated.root
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(message="Proxmox HA status request timed out", original_error=error)
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for HA status", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error fetching Proxmox HA status",
            original_error=error,
        )


@_dual_mode
async def get_ha_resources(
    session: ProxmoxSession,
    sid: str | None = None,
) -> list[dict[str, object]] | generated_models.GetClusterHaResourcesSidResponse:
    """Get HA resources from Proxmox.

    When ``sid`` is provided, fetches the full single-resource detail at
    `cluster/ha/resources/{sid}` and returns the validated Pydantic model.
    Otherwise lists all HA resources at `cluster/ha/resources`. The list
    endpoint upstream returns minimal rows (just `sid`); merge with
    `/cluster/ha/status/current` if you need state per resource.
    """
    try:
        if sid is None:
            result = await resolve_async(session.session("cluster/ha/resources").get())
            return result if isinstance(result, list) else []
        result = await resolve_async(session.session(f"cluster/ha/resources/{sid}").get())
        return generated_models.GetClusterHaResourcesSidResponse.model_validate(result)
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(
            message="Proxmox HA resources request timed out", original_error=error
        )
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for HA resources", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error fetching Proxmox HA resources",
            original_error=error,
        )


@_dual_mode
async def get_ha_groups(
    session: ProxmoxSession,
    group: str | None = None,
) -> list[dict[str, object]] | dict[str, object]:
    """Get HA groups from Proxmox.

    With no ``group``, returns the list of HA group rows. With a ``group``
    name, returns the full group detail dictionary (the upstream schema is
    a permissive ``dict[str, object]``).
    """
    try:
        if group is None:
            result = await resolve_async(session.session("cluster/ha/groups").get())
            return result if isinstance(result, list) else []
        result = await resolve_async(session.session(f"cluster/ha/groups/{group}").get())
        return result if isinstance(result, dict) else {}
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(message="Proxmox HA groups request timed out", original_error=error)
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for HA groups", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error fetching Proxmox HA groups",
            original_error=error,
        )


@_dual_mode
async def get_vm_config(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> (
    generated_models.GetNodesNodeQemuVmidConfigResponse
    | generated_models.GetNodesNodeLxcVmidConfigResponse
):
    """Get VM configuration from Proxmox."""
    try:
        if vm_type == "qemu":
            payload = await resolve_async(session.session.nodes(node).qemu(vmid).config.get())
            return generated_models.GetNodesNodeQemuVmidConfigResponse.model_validate(payload)
        if vm_type == "lxc":
            payload = await resolve_async(session.session.nodes(node).lxc(vmid).config.get())
            return generated_models.GetNodesNodeLxcVmidConfigResponse.model_validate(payload)
        raise ValueError(f"Unsupported VM type: {vm_type}")
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(message="Proxmox VM config request timed out", original_error=error)
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for VM config", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error fetching Proxmox VM config",
            original_error=error,
        )


def _normalize_guest_agent_interfaces(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, dict):
        raw_interfaces = payload.get("result") or payload.get("interfaces") or []
    elif isinstance(payload, list):
        raw_interfaces = payload
    else:
        raw_interfaces = []

    normalized: list[dict[str, object]] = []
    for item in raw_interfaces:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name:
            continue
        addresses: list[dict[str, object]] = []
        for addr in item.get("ip-addresses") or item.get("ip_addresses") or []:
            if not isinstance(addr, dict):
                continue
            ip_address = addr.get("ip-address") or addr.get("ip_address")
            if not ip_address:
                continue
            addresses.append(
                {
                    "ip_address": str(ip_address),
                    "prefix": addr.get("prefix"),
                    "ip_address_type": addr.get("ip-address-type") or addr.get("ip_address_type"),
                }
            )
        normalized.append(
            {
                "name": str(name),
                "mac_address": item.get("hardware-address") or item.get("hardware_address"),
                "ip_addresses": addresses,
            }
        )
    return normalized


@dataclass(frozen=True)
class GuestAgentFetchResult:
    """Outcome of a guest-agent network-get-interfaces call.

    ``interfaces`` is empty when the call failed or the agent returned no data.
    ``diagnostic`` carries a short, operator-facing reason for the empty list
    (None on success or when no diagnostic is meaningful).
    """

    interfaces: list[dict[str, object]]
    diagnostic: str | None = None


_GUEST_AGENT_PERMISSION_HINT = (
    "PVE rejected agent network-get-interfaces (permission denied). "
    "On PVE >= 9 the API-token role needs VM.GuestAgent.Audit in addition "
    "to VM.Monitor; on PVE 8 VM.Monitor alone is sufficient."
)


def _classify_guest_agent_error(error: Exception) -> tuple[str, str]:
    """Map a guest-agent fetch error to (log_level, operator_hint).

    log_level is one of "info", "warning", "error".
    """
    text = str(error).lower()
    status = getattr(error, "status", None) or getattr(error, "status_code", None)
    if status == 403 or any(
        token in text for token in ("forbidden", "permission denied", "not authorized")
    ):
        return "error", _GUEST_AGENT_PERMISSION_HINT
    if "guest agent is not running" in text or "guest-agent is not running" in text:
        return "info", "QEMU guest agent is not running in the VM."
    if "agent" in text and "not enabled" in text:
        return "info", "QEMU guest agent is not enabled in VM config."
    return "warning", f"Guest-agent fetch failed: {error}"


@_dual_mode
async def fetch_qemu_guest_agent_network_interfaces(
    session: ProxmoxSession,
    node: str,
    vmid: int,
) -> GuestAgentFetchResult:
    """Fetch and normalize guest-agent interfaces, with a structured diagnostic.

    Returns ``GuestAgentFetchResult(interfaces=[...], diagnostic=None)`` on
    success and ``GuestAgentFetchResult(interfaces=[], diagnostic="...")`` on
    failure. The diagnostic is suitable for surfacing to the SSE/WebSocket
    progress stream so operators see *why* IPs were not synced for a VM.
    """
    try:
        try:
            payload = await resolve_async(
                session.session.nodes(node).qemu(vmid).agent("network-get-interfaces").get()
            )
        except Exception as error:
            logger.debug(
                "Primary guest-agent interfaces call failed for node=%s vmid=%s: %s",
                node,
                vmid,
                error,
            )
            payload = await resolve_async(
                session.session.nodes(node).qemu(vmid).agent.get(command="network-get-interfaces")
            )
        return GuestAgentFetchResult(
            interfaces=_normalize_guest_agent_interfaces(payload),
            diagnostic=None,
        )
    except Exception as error:
        level, hint = _classify_guest_agent_error(error)
        log_fn = {
            "info": logger.info,
            "warning": logger.warning,
            "error": logger.error,
        }.get(level, logger.warning)
        log_fn(
            "Unable to fetch guest-agent interfaces for node=%s vmid=%s: %s (%s)",
            node,
            vmid,
            error,
            hint,
        )
        return GuestAgentFetchResult(interfaces=[], diagnostic=hint)


@_dual_mode
async def get_qemu_guest_agent_network_interfaces(
    session: ProxmoxSession,
    node: str,
    vmid: int,
) -> list[dict[str, object]]:
    """Return normalized guest-agent interfaces or [] when unavailable.

    Thin wrapper over :func:`fetch_qemu_guest_agent_network_interfaces` for
    callers that only need the interface list. New callers should prefer the
    structured variant so they can surface a diagnostic to the user.
    """
    result = await fetch_qemu_guest_agent_network_interfaces(session, node, vmid)
    return result.interfaces


def sanitize_dns_hostname(value: object) -> str | None:
    """Normalize a guest hostname into a NetBox-acceptable dns_name.

    Returns None when the value is empty or matches the localhost family.
    """
    if value in (None, ""):
        return None
    text = str(value).strip().rstrip(".").lower()
    if not text:
        return None
    if text == "localhost" or text.startswith("localhost."):
        return None
    return text[:255]


def _extract_hostname_from_payload(payload: object) -> str | None:
    """Pull a hostname out of an `agent/get-host-name` response shape."""
    candidates: list[object] = []
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, dict):
            candidates.extend(
                [result.get("host-name"), result.get("hostname"), result.get("host_name")]
            )
        candidates.extend(
            [payload.get("host-name"), payload.get("hostname"), payload.get("host_name")]
        )
    elif isinstance(payload, str):
        candidates.append(payload)
    for candidate in candidates:
        cleaned = sanitize_dns_hostname(candidate)
        if cleaned:
            return cleaned
    return None


def _extract_hostname_from_interfaces(interfaces: object) -> str | None:
    """Best-effort hostname/FQDN scan of normalized guest-agent interfaces."""
    if not isinstance(interfaces, list):
        return None
    best: str | None = None
    for item in interfaces:
        if not isinstance(item, dict):
            continue
        for key in ("fqdn", "hostname", "host-name", "host_name"):
            cleaned = sanitize_dns_hostname(item.get(key))
            if cleaned and (best is None or len(cleaned) > len(best)):
                best = cleaned
    return best


@_dual_mode
async def get_qemu_guest_agent_hostname(
    session: ProxmoxSession,
    node: str,
    vmid: int,
) -> str | None:
    """Return the guest-reported hostname or None when unavailable.

    Tries `agent/get-host-name` first (with the same dual-call fallback used
    by `get_qemu_guest_agent_network_interfaces`), then falls back to scanning
    the normalized network-interfaces payload for an FQDN/hostname-like
    field. Returns None on any failure so callers can stay terse.
    """
    try:
        payload: object | None = None
        try:
            payload = await resolve_async(
                session.session.nodes(node).qemu(vmid).agent("get-host-name").get()
            )
        except Exception as primary_error:
            logger.debug(
                "Primary guest-agent hostname call failed for node=%s vmid=%s: %s",
                node,
                vmid,
                primary_error,
            )
            try:
                payload = await resolve_async(
                    session.session.nodes(node).qemu(vmid).agent.get(command="get-host-name")
                )
            except Exception as fallback_error:
                logger.debug(
                    "Fallback guest-agent hostname call failed for node=%s vmid=%s: %s",
                    node,
                    vmid,
                    fallback_error,
                )
                payload = None

        hostname = _extract_hostname_from_payload(payload) if payload is not None else None
        if hostname:
            return hostname

        interfaces = await get_qemu_guest_agent_network_interfaces(session, node, vmid)
        return _extract_hostname_from_interfaces(interfaces)
    except Exception as error:
        logger.warning(
            "Unable to resolve guest-agent hostname for node=%s vmid=%s: %s",
            node,
            vmid,
            error,
        )
        return None


@_dual_mode
async def get_storage_list(
    session: ProxmoxSession,
) -> list[generated_models.GetStorageResponseItem]:
    """Get storage list from Proxmox."""
    try:
        result = await resolve_async(session.session.storage.get())
        validated = generated_models.GetStorageResponse.model_validate(result)
        return validated.root
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(
            message="Proxmox storage list request timed out", original_error=error
        )
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for storage list", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error fetching Proxmox storage list",
            original_error=error,
        )


@_dual_mode
async def get_storage_config(
    session: ProxmoxSession,
    storage_id: str,
) -> dict[str, object]:
    """Fetch full storage configuration from Proxmox.

    The /storage endpoint only returns storage IDs. This helper fetches
    the full configuration including type, content, path, nodes, shared, etc.
    """
    try:
        result = await resolve_async(session.session.storage(storage_id).get())
        validated = generated_models.GetStorageStorageResponse.model_validate(result)
        return _model_dump(validated)
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(
            message=f"Proxmox storage config request timed out for {storage_id}",
            original_error=error,
        )
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message=f"Unable to connect to Proxmox for storage config {storage_id}",
            original_error=error,
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message=f"Error fetching Proxmox storage config for {storage_id}",
            original_error=error,
        )


@_dual_mode
async def get_node_storage_content(
    session: ProxmoxSession,
    node: str,
    storage: str,
    **kwargs: object,
) -> list[generated_models.GetNodesNodeStorageStorageContentResponseItem]:
    """Get storage content from a specific node."""
    try:
        params = {key: value for key, value in kwargs.items() if value is not None}
        result = await resolve_async(
            session.session.nodes(node).storage(storage).content.get(**params)
        )
        validated = generated_models.GetNodesNodeStorageStorageContentResponse.model_validate(
            result
        )
        return validated.root
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(
            message="Proxmox node storage content request timed out", original_error=error
        )
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for node storage content", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error fetching Proxmox node storage content",
            original_error=error,
        )


@_dual_mode
async def get_node_tasks(
    session: ProxmoxSession,
    node: str,
    *,
    vmid: int | None = None,
    source: str | None = "archive",
    statusfilter: str | None = None,
    typefilter: str | None = None,
    until: int | None = None,
    userfilter: str | None = None,
) -> list[generated_models.GetNodesNodeTasksResponseItem]:
    """Get tasks from a specific node."""
    try:
        params = {
            "vmid": vmid,
            "source": source,
            "statusfilter": statusfilter,
            "typefilter": typefilter,
            "until": until,
            "userfilter": userfilter,
        }
        filtered = {key: value for key, value in params.items() if value is not None}
        result = await resolve_async(session.session.nodes(node).tasks.get(**filtered))
        validated = generated_models.GetNodesNodeTasksResponse.model_validate(result)
        return validated.root
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(message="Proxmox node tasks request timed out", original_error=error)
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for node tasks", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error fetching Proxmox node tasks",
            original_error=error,
        )


@_dual_mode
async def get_node_task_status(
    session: ProxmoxSession,
    node: str,
    upid: str,
) -> generated_models.GetNodesNodeTasksUpidStatusResponse:
    """Get status of a specific task."""
    try:
        result = await resolve_async(session.session.nodes(node).tasks(upid).status.get())
        return generated_models.GetNodesNodeTasksUpidStatusResponse.model_validate(result)
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(message="Proxmox task status request timed out", original_error=error)
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for task status", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error fetching Proxmox task status",
            original_error=error,
        )


@_dual_mode
async def get_vm_status(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> (
    generated_models.GetNodesNodeQemuVmidStatusCurrentResponse
    | generated_models.GetNodesNodeLxcVmidStatusCurrentResponse
):
    """Return current Proxmox VM run state (used for state-based no-op checks).

    Wraps ``GET /nodes/{node}/{vm_type}/{vmid}/status/current``. Per
    ``docs/design/operational-verbs.md`` §4.2, the verb routes call this
    before dispatch so a ``start`` against an already-running VM (or a
    ``stop`` against a stopped one) returns a no-op without consuming an
    Idempotency-Key window.
    """
    try:
        if vm_type == "qemu":
            payload = await resolve_async(
                session.session.nodes(node).qemu(vmid).status.current.get()
            )
            return generated_models.GetNodesNodeQemuVmidStatusCurrentResponse.model_validate(
                payload
            )
        if vm_type == "lxc":
            payload = await resolve_async(
                session.session.nodes(node).lxc(vmid).status.current.get()
            )
            return generated_models.GetNodesNodeLxcVmidStatusCurrentResponse.model_validate(payload)
        raise ValueError(f"Unsupported VM type: {vm_type}")
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(message="Proxmox VM status request timed out", original_error=error)
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for VM status", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error fetching Proxmox VM status",
            original_error=error,
        )


@_dual_mode
async def start_vm(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> str:
    """Dispatch ``POST /nodes/{node}/{vm_type}/{vmid}/status/start``.

    Returns the Proxmox task ``UPID`` string. ``ProxmoxAPIError`` is
    raised on timeout / connection failure per the existing convention.
    """
    try:
        if vm_type == "qemu":
            payload = await resolve_async(
                session.session.nodes(node).qemu(vmid).status.start.post()
            )
        elif vm_type == "lxc":
            payload = await resolve_async(session.session.nodes(node).lxc(vmid).status.start.post())
        else:
            raise ValueError(f"Unsupported VM type: {vm_type}")
        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, str):
                return data
        return str(payload)
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(message="Proxmox VM start request timed out", original_error=error)
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for VM start", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error dispatching Proxmox VM start",
            original_error=error,
        )


def dump_models(items: list[object]) -> list[dict[str, object]]:
    return [_model_dump(item) for item in items]


@_dual_mode
async def get_vm_snapshots(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> list[dict[str, object]]:
    """Get snapshots for a specific VM from Proxmox."""
    try:
        if vm_type == "qemu":
            payload = await resolve_async(session.session.nodes(node).qemu(vmid).snapshot.get())
        elif vm_type == "lxc":
            payload = await resolve_async(session.session.nodes(node).lxc(vmid).snapshot.get())
        else:
            raise ValueError(f"Unsupported VM type: {vm_type}")
        return payload if isinstance(payload, list) else []
    except ResourceException as error:
        if error.status_code == 501:
            logger.debug(
                "Snapshots not supported for vmid=%s node=%s type=%s (501 Not Implemented)",
                vmid,
                node,
                vm_type,
            )
        else:
            logger.warning(
                "Error fetching snapshots for vmid=%s node=%s type=%s: %s",
                vmid,
                node,
                vm_type,
                error,
            )
        return []
    except Exception as error:
        logger.warning(
            "Error fetching snapshots for vmid=%s node=%s type=%s: %s",
            vmid,
            node,
            vm_type,
            error,
        )
        return []


@_dual_mode
async def get_cluster_snapshots_for_vm(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> list[dict[str, object]]:
    """Get all snapshots for a VM across all nodes in the cluster."""
    all_snapshots = []

    try:
        snapshots = await get_vm_snapshots(session, node, vm_type, vmid)
        all_snapshots.extend(snapshots)
    except Exception as error:
        logger.warning(
            "Error aggregating cluster snapshots for vmid=%s node=%s type=%s: %s",
            vmid,
            node,
            vm_type,
            error,
        )

    return all_snapshots


@_dual_mode
async def get_node_status_individual(
    session: ProxmoxSession,
    node: str,
) -> dict[str, object]:
    """Get a single node's status from cluster status."""
    try:
        status_list = await get_cluster_status(session)
        for item in status_list:
            item_dict = _model_dump(item)
            if str(item_dict.get("node", "")) == node or str(item_dict.get("name", "")) == node:
                return item_dict
    except Exception as error:
        logger.warning(
            "Error fetching node status for node=%s: %s",
            node,
            error,
        )
    return {}


@_dual_mode
async def get_storage_config_individual(
    session: ProxmoxSession,
    storage_id: str,
) -> dict[str, object]:
    """Get storage configuration for a specific storage."""
    return await get_storage_config(session, storage_id)


async def get_vm_config_individual(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> dict[str, object]:
    """Get VM configuration for a specific VM."""
    config = await get_vm_config(session, node, vm_type, vmid)
    return _model_dump(config)


async def get_vm_snapshots_individual(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> list[dict[str, object]]:
    """Get snapshots for a specific VM."""
    return await get_vm_snapshots(session, node, vm_type, vmid)


async def get_vm_backups_individual(
    session: ProxmoxSession,
    node: str,
    storage: str,
    vmid: int,
) -> list[dict[str, object]]:
    """Get backups for a specific VM from storage content."""
    try:
        content = await get_node_storage_content(
            session, node, storage, vmid=str(vmid), content="backup"
        )
        backups = []
        for item in content:
            item_dict = _model_dump(item)
            item_vmid = item_dict.get("vmid")
            if item_vmid is not None and int(item_vmid) == vmid:
                backups.append(item_dict)
        return backups
    except Exception as error:
        logger.warning(
            "Error fetching backups for vmid=%s node=%s storage=%s: %s",
            vmid,
            node,
            storage,
            error,
        )
        return []


async def get_vm_tasks_individual(
    session: ProxmoxSession,
    node: str,
    vmid: int | None = None,
    source: str = "archive",
) -> list[dict[str, object]]:
    """Get tasks for a specific VM."""
    try:
        tasks = await get_node_tasks(session, node, vmid=vmid, source=source)
        task_dicts = [_model_dump(t) for t in tasks]
        if vmid is not None:
            filtered = []
            for task in task_dicts:
                task_vmid = task.get("vmid")
                if task_vmid is not None and int(task_vmid) == vmid:
                    filtered.append(task)
            return filtered
        return task_dicts
    except Exception as error:
        logger.warning(
            "Error fetching tasks for node=%s vmid=%s: %s",
            node,
            vmid,
            error,
        )
        return []


async def get_vm_resource_individual(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> dict[str, object]:
    """Get a single VM resource from cluster resources filtered by node and vmid."""
    try:
        resources = await get_cluster_resources(session)
        for resource in resources:
            resource_dict = _model_dump(resource)
            res_type = resource_dict.get("type", "")
            res_node = resource_dict.get("node", "")
            res_vmid = resource_dict.get("vmid")
            if (
                res_type == vm_type
                and res_node == node
                and res_vmid is not None
                and int(res_vmid) == vmid
            ):
                return resource_dict
    except Exception as error:
        logger.warning(
            "Error fetching VM resource for node=%s type=%s vmid=%s: %s",
            node,
            vm_type,
            vmid,
            error,
        )
    return {}

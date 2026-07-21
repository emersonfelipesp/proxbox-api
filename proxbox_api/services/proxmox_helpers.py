"""Typed helpers for proxmox-sdk calls validated through generated models."""

from __future__ import annotations

import asyncio
import functools
import re
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
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


def _task_upid_from_payload(payload: object) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, str):
            return data
    return str(payload)


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
            # ``ClusterResourcesType`` is a ``(str, Enum)``. Passing the enum
            # member straight through urlencodes it as ``ClusterResourcesType.vm``
            # (str(member) -> "ClusterResourcesType.vm"), which Proxmox rejects
            # with ``HTTP 400 Parameter verification failed``. Send the plain
            # value ("vm"/"node"/...) instead.
            type_param = (
                resource_type.value if isinstance(resource_type, Enum) else str(resource_type)
            )
            result = await resolve_async(session.session("cluster/resources").get(type=type_param))
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

    PVE 9.x note: ``cluster/ha/groups`` was removed in favour of
    ``cluster/ha/rules`` and returns HTTP 500 with "ha groups have been
    migrated to rules".  When that specific error is detected the helper
    returns an empty result (list or dict) at DEBUG level so callers degrade
    gracefully instead of surfacing a noisy ERROR traceback.
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
    except ResourceException as exc:
        # PVE 9.x removed cluster/ha/groups — degrade gracefully instead of surfacing an error.
        if exc.status_code == 500 and "migrated to rules" in str(exc).lower():
            logger.debug(
                "cluster/ha/groups not available on this node (PVE 9.x+, migrated to rules): %s",
                exc,
            )
            return [] if group is None else {}
        raise ProxmoxAPIError(
            message="Proxmox HA groups request failed",
            original_error=exc,
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error fetching Proxmox HA groups",
            original_error=error,
        )


@_dual_mode
async def get_ha_rules(
    session: ProxmoxSession,
    rule: str | None = None,
) -> list[dict[str, object]] | dict[str, object]:
    """Get HA rules from Proxmox (PVE 9.x+).

    ``cluster/ha/groups`` was replaced by ``cluster/ha/rules`` in PVE 9.x.
    With no ``rule``, returns the list of rule rows.  With a ``rule``
    identifier, returns the full rule detail dictionary.
    """
    try:
        if rule is None:
            result = await resolve_async(session.session("cluster/ha/rules").get())
            return result if isinstance(result, list) else []
        result = await resolve_async(session.session(f"cluster/ha/rules/{rule}").get())
        return result if isinstance(result, dict) else {}
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(message="Proxmox HA rules request timed out", original_error=error)
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for HA rules", original_error=error
        )
    except ResourceException as exc:
        # cluster/ha/rules does not exist on PVE < 9.x — degrade gracefully.
        logger.debug(
            "cluster/ha/rules not available on this node (PVE < 9.x or endpoint absent): %s",
            exc,
        )
        return [] if rule is None else {}
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error fetching Proxmox HA rules",
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


# Guest-agent alias entries are named "<parent>:<N>" (e.g. "ens20:1").
_GUEST_AGENT_ALIAS_RE = re.compile(r"^(?P<base>.+):\d+$")


def _alias_base_name(name: str) -> str | None:
    """Return the parent interface name for an alias "<base>:<N>", else None."""
    match = _GUEST_AGENT_ALIAS_RE.match(name)
    return match.group("base") if match else None


def _aggregate_guest_agent_interfaces(
    interfaces: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Merge guest-agent alias entries into their parent interface, by name.

    Linux alias interfaces are named ``"<parent>:<N>"`` (e.g. ``ens20:1``) and
    belong to the interface named ``<parent>``. Aliases are matched to their
    parent by *name* — not by MAC — so genuine distinct interfaces that happen
    to share a MAC (e.g. real VRRP virtual interfaces) are never conflated.
    Each alias's addresses are merged into the canonical entry (deduped by
    ``(ip_address, prefix)``, order preserved) and the alias entry is dropped.
    The canonical entry is the non-alias interface named ``<parent>`` if present;
    otherwise (an alias-only group) the first alias of that parent name is kept
    and the rest merge into it. Non-alias interfaces are returned unchanged.
    """
    # First non-alias interface index per name (the parent candidate).
    parent_by_name: dict[str, int] = {}
    for idx, iface in enumerate(interfaces):
        name = str(iface.get("name", ""))
        if _alias_base_name(name) is None:
            parent_by_name.setdefault(name, idx)

    # Canonical target index per parent name for alias-only groups.
    alias_canonical_by_base: dict[str, int] = {}
    drop: set[int] = set()
    for idx, iface in enumerate(interfaces):
        base = _alias_base_name(str(iface.get("name", "")))
        if base is None:
            continue
        canonical = parent_by_name.get(base)
        if canonical is None:
            # No real parent: keep the first alias of this base as canonical.
            canonical = alias_canonical_by_base.setdefault(base, idx)
        if canonical == idx:
            continue
        _merge_interface_addresses(interfaces[canonical], interfaces[idx])
        drop.add(idx)

    if not drop:
        return interfaces
    return [iface for idx, iface in enumerate(interfaces) if idx not in drop]


def _merge_interface_addresses(
    canonical: dict[str, object],
    source: dict[str, object],
) -> None:
    """Merge ``source``'s ip_addresses into ``canonical`` (deduped, order kept)."""
    canonical_addresses = canonical.setdefault("ip_addresses", [])
    if not isinstance(canonical_addresses, list):
        return
    seen = {
        (addr.get("ip_address"), addr.get("prefix"))
        for addr in canonical_addresses
        if isinstance(addr, dict)
    }
    for addr in source.get("ip_addresses") or []:
        if not isinstance(addr, dict):
            continue
        key = (addr.get("ip_address"), addr.get("prefix"))
        if key not in seen:
            seen.add(key)
            canonical_addresses.append(addr)


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
    return _aggregate_guest_agent_interfaces(normalized)


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

_GUEST_AGENT_TIMEOUT_HINT = (
    "Guest-agent network-get-interfaces timed out. Interface-dense guests "
    "(many VRRP/alias interfaces) can take long to enumerate; raise "
    "PROXBOX_GUEST_AGENT_TIMEOUT (plugin key guest_agent_timeout) if this persists."
)


def _resolve_guest_agent_timeout() -> int:
    """Dedicated timeout (seconds) for guest-agent network-get-interfaces calls.

    Resolution: env PROXBOX_GUEST_AGENT_TIMEOUT > ProxboxPluginSettings
    guest_agent_timeout > default 15. Falls back gracefully when the plugin
    settings key does not exist yet.
    """
    from proxbox_api import runtime_settings

    return runtime_settings.get_int(
        settings_key="guest_agent_timeout",
        env="PROXBOX_GUEST_AGENT_TIMEOUT",
        default=15,
        minimum=1,
        maximum=600,
    )


@contextmanager
def _scoped_proxmox_backend_timeout(session: ProxmoxSession, timeout_s: float):
    """Temporarily widen the HTTPS backend's request timeout.

    proxmox-sdk has no per-call timeout; the HTTPS backend applies one
    session-level ``aiohttp.ClientTimeout``. Guest-agent enumeration on
    interface-dense guests can legitimately exceed the default 5 s, so the
    timeout's ``total`` is widened to ``max(original_total, timeout_s)`` for the
    duration of the agent call and the other timeout fields (``connect``,
    ``sock_connect``, ``sock_read``) are preserved. It only ever widens, never
    shortens: an existing larger ``total`` and an unbounded (``None``) ``total``
    are left untouched.

    Because the backend session is shared, overlapping guest-agent calls are
    tracked with a depth counter on the backend (asyncio is single-threaded, so
    the counter is consistent between awaits): the first entrant records the
    true original timeout and the last exitant restores it, so a concurrent call
    can never restore a stale value or shorten the window mid-flight. Degrades
    to a no-op for mock/pvesh/SSH backends without a ``_timeout`` attribute.
    """
    backend = getattr(getattr(session, "session", None), "_backend", None)
    if backend is None or getattr(backend, "_timeout", None) is None:
        yield
        return

    try:
        import aiohttp

        depth = getattr(backend, "_proxbox_timeout_depth", 0)
        if depth == 0:
            backend._proxbox_timeout_original = backend._timeout
        original = backend._proxbox_timeout_original
        old_total = getattr(original, "total", None)
        # Only widen: keep an unbounded total, and never lower an existing one.
        if old_total is not None and old_total < timeout_s:
            backend._timeout = aiohttp.ClientTimeout(
                total=timeout_s,
                connect=getattr(original, "connect", None),
                sock_connect=getattr(original, "sock_connect", None),
                sock_read=getattr(original, "sock_read", None),
            )
        backend._proxbox_timeout_depth = depth + 1
    except Exception:  # noqa: BLE001 - never let the override break the fetch
        yield
        return

    try:
        yield
    finally:
        new_depth = getattr(backend, "_proxbox_timeout_depth", 1) - 1
        backend._proxbox_timeout_depth = new_depth
        if new_depth <= 0:
            backend._timeout = getattr(backend, "_proxbox_timeout_original", backend._timeout)
            backend._proxbox_timeout_depth = 0


def _is_timeout_error(error: Exception) -> bool:
    if isinstance(error, asyncio.TimeoutError | TimeoutError | ProxmoxTimeoutError):
        return True
    return "timed out" in str(error).lower() or "timeout" in str(error).lower()


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
    if _is_timeout_error(error):
        return "warning", _GUEST_AGENT_TIMEOUT_HINT
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
    timeout_s = _resolve_guest_agent_timeout()

    async def _primary_call() -> object:
        with _scoped_proxmox_backend_timeout(session, timeout_s):
            return await asyncio.wait_for(
                resolve_async(
                    session.session.nodes(node).qemu(vmid).agent("network-get-interfaces").get()
                ),
                timeout=timeout_s,
            )

    try:
        try:
            try:
                payload = await _primary_call()
            except Exception as primary_error:
                if not _is_timeout_error(primary_error):
                    raise
                # One bounded retry: interface-dense guests can be slow to
                # enumerate and a single timeout is often transient.
                logger.warning(
                    "Guest-agent interfaces call timed out after %ss for node=%s vmid=%s "
                    "-- retrying once",
                    timeout_s,
                    node,
                    vmid,
                )
                payload = await _primary_call()
        except Exception as error:
            if _is_timeout_error(error):
                raise
            logger.debug(
                "Primary guest-agent interfaces call failed for node=%s vmid=%s: %s",
                node,
                vmid,
                error,
            )
            with _scoped_proxmox_backend_timeout(session, timeout_s):
                payload = await asyncio.wait_for(
                    resolve_async(
                        session.session.nodes(node)
                        .qemu(vmid)
                        .agent.get(command="network-get-interfaces")
                    ),
                    timeout=timeout_s,
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
async def get_node_zfs_pools(
    session: ProxmoxSession,
    node: str,
) -> list[generated_models.GetNodesNodeDisksZfsResponseItem]:
    """Get ZFS pool summaries from a Proxmox node via the structured REST API."""
    try:
        result = await resolve_async(session.session.nodes(node).disks.zfs.get())
        validated = generated_models.GetNodesNodeDisksZfsResponse.model_validate(result)
        return validated.root
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(
            message=f"Proxmox node ZFS pool list request timed out for {node}",
            original_error=error,
        )
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message=f"Unable to connect to Proxmox for node ZFS pool list on {node}",
            original_error=error,
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message=f"Error fetching Proxmox ZFS pool list for node {node}",
            original_error=error,
        )


@_dual_mode
async def get_node_zfs_pool_detail(
    session: ProxmoxSession,
    node: str,
    name: str,
) -> generated_models.GetNodesNodeDisksZfsNameResponse:
    """Get ZFS pool detail and vdev topology from the structured Proxmox REST API."""
    try:
        result = await resolve_async(session.session.nodes(node).disks.zfs(name).get())
        return generated_models.GetNodesNodeDisksZfsNameResponse.model_validate(result)
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(
            message=f"Proxmox node ZFS pool detail request timed out for {node}/{name}",
            original_error=error,
        )
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message=f"Unable to connect to Proxmox for ZFS pool detail {node}/{name}",
            original_error=error,
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message=f"Error fetching Proxmox ZFS pool detail for {node}/{name}",
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


@_dual_mode
async def stop_vm(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> str:
    """Dispatch ``POST /nodes/{node}/{vm_type}/{vmid}/status/stop``.

    Returns the Proxmox task ``UPID`` string. ``ProxmoxAPIError`` is
    raised on timeout / connection failure per the existing convention.
    """
    try:
        if vm_type == "qemu":
            payload = await resolve_async(session.session.nodes(node).qemu(vmid).status.stop.post())
        elif vm_type == "lxc":
            payload = await resolve_async(session.session.nodes(node).lxc(vmid).status.stop.post())
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
        raise ProxmoxAPIError(message="Proxmox VM stop request timed out", original_error=error)
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for VM stop", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error dispatching Proxmox VM stop",
            original_error=error,
        )


@_dual_mode
async def reboot_vm(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> str:
    """Dispatch ``POST /nodes/{node}/{vm_type}/{vmid}/status/reboot``.

    Returns the Proxmox task ``UPID`` string. ``ProxmoxAPIError`` is
    raised on timeout / connection failure per the existing convention.
    """
    try:
        if vm_type == "qemu":
            payload = await resolve_async(
                session.session.nodes(node).qemu(vmid).status.reboot.post()
            )
        elif vm_type == "lxc":
            payload = await resolve_async(
                session.session.nodes(node).lxc(vmid).status.reboot.post()
            )
        else:
            raise ValueError(f"Unsupported VM type: {vm_type}")
        return _task_upid_from_payload(payload)
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(message="Proxmox VM reboot request timed out", original_error=error)
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for VM reboot", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error dispatching Proxmox VM reboot",
            original_error=error,
        )


@_dual_mode
async def create_vm_snapshot(  # noqa: C901
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
    snapname: str,
    description: str | None = None,
) -> str:
    """Dispatch ``POST /nodes/{node}/{vm_type}/{vmid}/snapshot``.

    Returns the Proxmox task ``UPID`` string. ``ProxmoxAPIError`` is
    raised on timeout / connection failure per the existing convention.
    """
    body: dict[str, object] = {"snapname": snapname}
    if description is not None:
        body["description"] = description
    try:
        if vm_type == "qemu":
            payload = await resolve_async(
                session.session.nodes(node).qemu(vmid).snapshot.post(**body)
            )
        elif vm_type == "lxc":
            payload = await resolve_async(
                session.session.nodes(node).lxc(vmid).snapshot.post(**body)
            )
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
        raise ProxmoxAPIError(message="Proxmox VM snapshot request timed out", original_error=error)
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for VM snapshot", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error dispatching Proxmox VM snapshot",
            original_error=error,
        )


@_dual_mode
async def backup_vm(
    session: ProxmoxSession,
    node: str,
    vmid: int,
    storage: str,
    mode: str = "snapshot",
    compress: str = "zstd",
    notes: str | None = None,
) -> str:
    """Dispatch ``POST /nodes/{node}/vzdump`` for one guest.

    Returns the Proxmox task ``UPID`` string. ``notes`` maps to
    Proxmox's ``notes-template`` parameter.
    """
    body: dict[str, object] = {
        "vmid": str(vmid),
        "storage": storage,
        "mode": mode,
        "compress": compress,
    }
    if notes is not None:
        body["notes-template"] = notes
    try:
        payload = await resolve_async(session.session.nodes(node).vzdump.post(**body))
        return _task_upid_from_payload(payload)
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(message="Proxmox VM backup request timed out", original_error=error)
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for VM backup", original_error=error
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error dispatching Proxmox VM backup",
            original_error=error,
        )


@_dual_mode
async def delete_vm_snapshot(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
    snapname: str,
) -> str:
    """Dispatch ``DELETE /nodes/{node}/{vm_type}/{vmid}/snapshot/{snapname}``.

    Returns the Proxmox task ``UPID`` string. ``ProxmoxAPIError`` is
    raised on timeout / connection failure per the existing convention.
    """
    try:
        if vm_type == "qemu":
            payload = await resolve_async(
                session.session.nodes(node).qemu(vmid).snapshot(snapname).delete()
            )
        elif vm_type == "lxc":
            payload = await resolve_async(
                session.session.nodes(node).lxc(vmid).snapshot(snapname).delete()
            )
        else:
            raise ValueError(f"Unsupported VM type: {vm_type}")
        return _task_upid_from_payload(payload)
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(
            message="Proxmox VM snapshot delete request timed out", original_error=error
        )
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for VM snapshot delete",
            original_error=error,
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error dispatching Proxmox VM snapshot delete",
            original_error=error,
        )


@_dual_mode
async def migrate_preflight(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
) -> dict[str, object]:
    """Wrap ``GET /nodes/{node}/{vm_type}/{vmid}/migrate``.

    Returns a dict with ``allowed_nodes``, ``local_disks``,
    ``local_resources`` and ``running`` per ``operational-verbs.md`` §9.
    The route uses this to reject the migrate POST with 400 before any
    state mutation when ``target`` is not in ``allowed_nodes`` or when
    online migration is blocked by local disks / resources.
    """
    try:
        if vm_type == "qemu":
            payload = await resolve_async(session.session.nodes(node).qemu(vmid).migrate.get())
        elif vm_type == "lxc":
            payload = await resolve_async(session.session.nodes(node).lxc(vmid).migrate.get())
        else:
            raise ValueError(f"Unsupported VM type: {vm_type}")
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(message="Proxmox migrate preflight timed out", original_error=error)
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for migrate preflight",
            original_error=error,
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error fetching Proxmox migrate preflight",
            original_error=error,
        )
    # Proxmox wraps responses in either {data: ...} or returns the dict
    # directly depending on the backend (HTTPS vs pvesh). Normalise so the
    # caller can index allowed_nodes/local_disks/local_resources/running.
    if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], dict):
        payload = payload["data"]
    return payload if isinstance(payload, dict) else {}


@_dual_mode
async def migrate_vm(
    session: ProxmoxSession,
    node: str,
    vm_type: str,
    vmid: int,
    target: str,
    online: bool = False,
) -> str:
    """Dispatch ``POST /nodes/{node}/{vm_type}/{vmid}/migrate``.

    Returns the Proxmox task ``UPID`` string. For QEMU the ``online``
    flag enables live migration; for LXC the equivalent flag is
    ``restart`` (Proxmox restarts the container at the target node).
    """
    try:
        if vm_type == "qemu":
            payload = await resolve_async(
                session.session.nodes(node)
                .qemu(vmid)
                .migrate.post(target=target, online=1 if online else 0)
            )
        elif vm_type == "lxc":
            payload = await resolve_async(
                session.session.nodes(node)
                .lxc(vmid)
                .migrate.post(target=target, restart=1 if online else 0)
            )
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
        raise ProxmoxAPIError(message="Proxmox VM migrate request timed out", original_error=error)
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for VM migrate",
            original_error=error,
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error dispatching Proxmox VM migrate",
            original_error=error,
        )


@_dual_mode
async def cancel_task(
    session: ProxmoxSession,
    node: str,
    upid: str,
) -> None:
    """Wrap ``DELETE /nodes/{node}/tasks/{upid}``.

    Best-effort cancellation per ``operational-verbs.md`` §5: Proxmox
    decides whether the in-flight task can be torn down.
    """
    try:
        await resolve_async(session.session.nodes(node).tasks(upid).delete())
    except ProxboxException:
        raise
    except ProxmoxTimeoutError as error:
        raise ProxmoxAPIError(message="Proxmox task cancel request timed out", original_error=error)
    except ProxmoxConnectionError as error:
        raise ProxmoxAPIError(
            message="Unable to connect to Proxmox for task cancel",
            original_error=error,
        )
    except Exception as error:
        raise ProxmoxAPIError(
            message="Error cancelling Proxmox task",
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

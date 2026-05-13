"""SSH-based hardware discovery orchestrator.

Pure consumer of :mod:`proxmox_sdk.ssh` and :mod:`proxmox_sdk.node.hardware`.
Owns no SSH primitives of its own — the static guard test
``tests/test_hardware_discovery_no_paramiko_import.py`` walks every file under
``proxbox_api/`` and rejects any ``import paramiko``. The SSH boundary lives in
``proxmox-sdk`` so this orchestrator stays a thin sequencer that:

1. Reads ``hardware_discovery_enabled`` from :mod:`proxbox_api.settings_client`.
2. Fetches the per-node SSH credential from the netbox-proxbox plugin REST
   endpoint (HTTPS + Bearer / NetBox token).
3. Composes ``RemoteSSHClient`` + ``discover_node`` once per node, sequentially.
4. Reflects parsed facts onto NetBox ``dcim.Device.custom_fields`` and each
   ``dcim.Interface.custom_fields`` via the existing REST helpers.
5. Emits typed SSE frames (``hardware_discovery`` or ``item_progress`` with a
   ``warning``) for every node so the UI shows live progress.

Failures never abort the rest of the run — each node is independently
``try``/``except``-wrapped and emits a warning frame.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from netbox_sdk.config import authorization_header_value

from proxbox_api.logger import logger
from proxbox_api.settings_client import get_settings

if TYPE_CHECKING:
    from netbox_sdk.facade import Api

    from proxbox_api.utils.streaming import WebSocketSSEBridge


__all__ = [
    "HardwareDiscoveryError",
    "MissingCredential",
    "NodeSSHCredential",
    "fetch_credential",
    "is_enabled",
    "reflect_to_netbox",
    "run_for_nodes",
]


class HardwareDiscoveryError(Exception):
    """Base class for hardware-discovery orchestrator failures."""


class MissingCredential(HardwareDiscoveryError):
    """Raised when no SSH credential is registered for a given node."""


@dataclass(frozen=True)
class NodeSSHCredential:
    """Decrypted SSH credential record fetched from netbox-proxbox.

    Mirrors the response shape of
    ``/api/plugins/proxbox/ssh-credentials/by-node/{node_id}/credentials/``.
    """

    node_id: int
    host: str
    username: str
    known_host_fingerprint: str
    port: int = 22
    private_key: str | None = None
    password: str | None = None
    sudo_required: bool = True


def is_enabled() -> bool:
    """Return True when the netbox-proxbox plugin opted-in to hardware discovery.

    Resolution order matches every other proxbox tunable: env override is not
    supported (this is a UI-only toggle), plugin settings are authoritative,
    the default is ``False`` so unconfigured installs open zero SSH sockets.
    """
    settings = get_settings()
    return bool(settings.get("hardware_discovery_enabled", False))


def _credential_url(base_url: str, node_id: int) -> str:
    return (
        f"{base_url.rstrip('/')}"
        f"/api/plugins/proxbox/ssh-credentials/by-node/{int(node_id)}/credentials/"
    )


def _coerce_credential(node_id: int, host: str, payload: dict[str, Any]) -> NodeSSHCredential:
    fingerprint = str(payload.get("known_host_fingerprint") or "").strip()
    if not fingerprint:
        raise HardwareDiscoveryError(
            f"SSH credential for node {node_id} is missing known_host_fingerprint"
        )
    username = str(payload.get("username") or "").strip()
    if not username:
        raise HardwareDiscoveryError(f"SSH credential for node {node_id} is missing username")
    return NodeSSHCredential(
        node_id=int(payload.get("node_id") or node_id),
        host=host,
        username=username,
        known_host_fingerprint=fingerprint,
        port=int(payload.get("port") or 22),
        private_key=payload.get("private_key") or None,
        password=payload.get("password") or None,
        sudo_required=bool(payload.get("sudo_required", True)),
    )


def fetch_credential(  # noqa: C901 — sequential transport branches read top-down
    netbox_session: Api,
    node_id: int,
    host: str,
    *,
    timeout: float = 10.0,
) -> NodeSSHCredential:
    """Fetch the per-node SSH credential from netbox-proxbox over HTTPS+Bearer.

    Raises :class:`MissingCredential` on 404 (no credential registered) and
    :class:`HardwareDiscoveryError` on any other transport or shape error.
    The fetched secret material lives only on the returned dataclass; this
    function does **not** log payload bodies.
    """
    config = netbox_session.client.config
    base_url = (config.base_url or "").rstrip("/")
    if not base_url:
        raise HardwareDiscoveryError("NetBox base_url is not configured")

    auth = authorization_header_value(config)
    if not auth:
        raise HardwareDiscoveryError("NetBox auth header could not be built — token not configured")

    url = _credential_url(base_url, node_id)
    parsed = urllib.parse.urlsplit(url)
    req = urllib.request.Request(
        url,
        headers={"Authorization": auth, "Accept": "application/json"},
    )

    urlopen_kwargs: dict[str, object] = {"timeout": timeout}
    if parsed.scheme.lower() == "https" and getattr(config, "ssl_verify", True) is False:
        urlopen_kwargs["context"] = ssl._create_unverified_context()

    try:
        with urllib.request.urlopen(req, **urlopen_kwargs) as resp:
            if resp.status == 404:
                raise MissingCredential(f"No SSH credential registered for node {node_id}")
            if resp.status != 200:
                raise HardwareDiscoveryError(
                    f"Credential fetch for node {node_id} returned HTTP {resp.status}"
                )
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise MissingCredential(f"No SSH credential registered for node {node_id}") from exc
        raise HardwareDiscoveryError(
            f"Credential fetch for node {node_id} failed: HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise HardwareDiscoveryError(
            f"Credential fetch for node {node_id} failed: {exc.reason!s}"
        ) from exc

    try:
        payload = json.loads(body.decode())
    except json.JSONDecodeError as exc:
        raise HardwareDiscoveryError(
            f"Credential fetch for node {node_id} returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise HardwareDiscoveryError(
            f"Credential fetch for node {node_id} returned non-object payload"
        )
    return _coerce_credential(node_id, host, payload)


async def reflect_to_netbox(
    netbox_session: Api,
    node_id: int,
    facts: Any,
    interface_lookup: dict[str, int] | None = None,
) -> None:
    """Reflect parsed :class:`HardwareFacts` onto NetBox custom fields.

    Chassis fields land on ``dcim.devices/{node_id}``; per-NIC fields land on
    each matching ``dcim.interfaces/{interface_id}`` resolved via the
    ``interface_lookup`` ``{nic_name: interface_id}`` map. NICs without a
    matching interface are silently skipped (the device-sync pass owns
    interface lifecycle).
    """
    from proxbox_api.netbox_rest import rest_patch_async

    chassis_payload: dict[str, object] = {
        "custom_fields": {
            "hardware_chassis_serial": getattr(facts.chassis, "serial_number", None),
            "hardware_chassis_manufacturer": getattr(facts.chassis, "manufacturer", None),
            "hardware_chassis_product": getattr(facts.system, "product_name", None),
        }
    }
    await rest_patch_async(
        netbox_session,
        "/api/dcim/devices/",
        int(node_id),
        chassis_payload,
    )

    if not interface_lookup:
        return

    for nic in getattr(facts, "nics", ()) or ():
        iface_id = interface_lookup.get(getattr(nic, "name", ""))
        if iface_id is None:
            continue
        ethtool = getattr(nic, "ethtool", None)
        speed_gbps = getattr(nic, "speed_gbps", None)
        duplex = getattr(ethtool, "duplex", None) if ethtool is not None else None
        link = getattr(ethtool, "link_detected", None) if ethtool is not None else None
        iface_payload: dict[str, object] = {
            "custom_fields": {
                "nic_speed_gbps": speed_gbps,
                "nic_duplex": duplex,
                "nic_link": link,
            }
        }
        await rest_patch_async(
            netbox_session,
            "/api/dcim/interfaces/",
            int(iface_id),
            iface_payload,
        )


async def run_for_nodes(  # noqa: C901 — sequential per-node state machine with named branches
    netbox_session: Api,
    nodes: list[dict[str, Any]],
    *,
    bridge: WebSocketSSEBridge | None = None,
    interface_lookup_by_node: dict[int, dict[str, int]] | None = None,
) -> None:
    """Run hardware discovery for every node, sequentially.

    Each ``nodes`` entry must carry at least ``id`` (NetBox device id),
    ``name`` (Proxmox node name), and ``host`` (primary IP). Optional
    ``cluster`` is forwarded into the emitted SSE frame.

    Sequential dispatch is intentional — a stalled SSH session must not
    starve the rest of the run, and the existing connection pool sized for
    NetBox writes is already saturated under heavy sync load.

    When the global flag is off (``hardware_discovery_enabled=False``), this
    function is a no-op and never imports ``proxmox_sdk.ssh``.
    """
    if not is_enabled():
        return

    # Deferred imports keep ``import proxbox_api.services.hardware_discovery``
    # cheap (no paramiko transport setup) for the flag-off path and any test
    # that wants to assert the orchestrator never instantiates a client.
    from proxmox_sdk.node.hardware import discover_node
    from proxmox_sdk.ssh import (
        CommandNotAllowed,
        HostKeyMismatch,
        OutputTooLarge,
        RemoteSSHClient,
        SshAuthFailed,
        SshTimeout,
    )

    interface_lookup_by_node = interface_lookup_by_node or {}

    for node in nodes:
        node_id_raw = node.get("id") or node.get("netbox_id")
        if node_id_raw is None:
            continue
        try:
            node_id = int(node_id_raw)
        except (TypeError, ValueError):
            continue
        node_name = str(node.get("name") or f"node-{node_id}")
        host = str(node.get("host") or node.get("primary_ip") or "").strip()
        cluster_name = node.get("cluster")

        item_descriptor = {
            "name": node_name,
            "type": "node",
            "cluster": cluster_name,
            "netbox_id": node_id,
        }

        if not host:
            if bridge is not None:
                await bridge.emit_item_progress(
                    phase="hardware_discovery",
                    item=item_descriptor,
                    operation="skipped",
                    status="failed",
                    message=f"Skipping {node_name}: no primary IP",
                    progress_current=0,
                    progress_total=len(nodes),
                    warning="hardware_discovery_no_primary_ip",
                )
            continue

        try:
            cred = fetch_credential(netbox_session, node_id, host)
        except MissingCredential:
            if bridge is not None:
                await bridge.emit_item_progress(
                    phase="hardware_discovery",
                    item=item_descriptor,
                    operation="skipped",
                    status="failed",
                    message=f"No SSH credential registered for {node_name}",
                    progress_current=0,
                    progress_total=len(nodes),
                    warning="hardware_discovery_no_credential",
                )
            continue
        except HardwareDiscoveryError as exc:
            logger.warning("Hardware discovery credential fetch failed for %s: %s", node_name, exc)
            if bridge is not None:
                await bridge.emit_item_progress(
                    phase="hardware_discovery",
                    item=item_descriptor,
                    operation="failed",
                    status="failed",
                    message=f"Credential fetch failed for {node_name}",
                    progress_current=0,
                    progress_total=len(nodes),
                    warning="hardware_discovery_credential_fetch_failed",
                )
            continue

        started = time.monotonic()
        try:
            async with RemoteSSHClient(
                host=cred.host,
                port=cred.port,
                username=cred.username,
                known_host_fingerprint=cred.known_host_fingerprint,
                private_key=cred.private_key,
                password=cred.password,
                sudo=cred.sudo_required,
                command_allowlist=["dmidecode", "ip", "ethtool"],
            ) as ssh:
                facts = await discover_node(ssh)
        except HostKeyMismatch:
            if bridge is not None:
                await bridge.emit_item_progress(
                    phase="hardware_discovery",
                    item=item_descriptor,
                    operation="failed",
                    status="failed",
                    message=f"Host-key mismatch for {node_name}",
                    progress_current=0,
                    progress_total=len(nodes),
                    warning="host_key_mismatch",
                )
            continue
        except SshTimeout:
            if bridge is not None:
                await bridge.emit_item_progress(
                    phase="hardware_discovery",
                    item=item_descriptor,
                    operation="failed",
                    status="failed",
                    message=f"SSH timeout for {node_name}",
                    progress_current=0,
                    progress_total=len(nodes),
                    warning="hardware_discovery_timeout",
                )
            continue
        except SshAuthFailed:
            if bridge is not None:
                await bridge.emit_item_progress(
                    phase="hardware_discovery",
                    item=item_descriptor,
                    operation="failed",
                    status="failed",
                    message=f"SSH authentication failed for {node_name}",
                    progress_current=0,
                    progress_total=len(nodes),
                    warning="hardware_discovery_auth_failed",
                )
            continue
        except (CommandNotAllowed, OutputTooLarge) as exc:
            logger.warning("Hardware discovery transport rejected for %s: %s", node_name, exc)
            if bridge is not None:
                await bridge.emit_item_progress(
                    phase="hardware_discovery",
                    item=item_descriptor,
                    operation="failed",
                    status="failed",
                    message=f"Discovery rejected for {node_name}",
                    progress_current=0,
                    progress_total=len(nodes),
                    warning="hardware_discovery_transport_rejected",
                )
            continue
        except Exception as exc:  # noqa: BLE001 — sequential pass must survive arbitrary failures
            logger.warning("Hardware discovery failed for %s: %s", node_name, exc)
            if bridge is not None:
                await bridge.emit_item_progress(
                    phase="hardware_discovery",
                    item=item_descriptor,
                    operation="failed",
                    status="failed",
                    message=f"Discovery failed for {node_name}",
                    progress_current=0,
                    progress_total=len(nodes),
                    warning=f"hardware_discovery_failed: {type(exc).__name__}",
                )
            continue

        duration_ms = int((time.monotonic() - started) * 1000)

        try:
            await reflect_to_netbox(
                netbox_session,
                node_id,
                facts,
                interface_lookup=interface_lookup_by_node.get(node_id),
            )
        except Exception as exc:  # noqa: BLE001 — reflect failure must not break the run
            logger.warning("Hardware discovery reflect failed for %s: %s", node_name, exc)
            if bridge is not None:
                await bridge.emit_item_progress(
                    phase="hardware_discovery",
                    item=item_descriptor,
                    operation="failed",
                    status="failed",
                    message=f"Reflect failed for {node_name}",
                    progress_current=0,
                    progress_total=len(nodes),
                    warning="hardware_discovery_reflect_failed",
                )
            continue

        if bridge is not None:
            await bridge.emit_hardware_discovery_progress(
                node=node_name,
                cluster=str(cluster_name) if cluster_name is not None else None,
                chassis_serial=getattr(facts.chassis, "serial_number", None),
                chassis_manufacturer=getattr(facts.chassis, "manufacturer", None),
                chassis_product=getattr(facts.system, "product_name", None),
                nic_count=len(getattr(facts, "nics", ()) or ()),
                duration_ms=duration_ms,
            )

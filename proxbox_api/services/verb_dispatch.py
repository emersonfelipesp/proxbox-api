"""Shared infrastructure for operational verb routes (issue #376).

Per ``docs/design/operational-verbs.md`` §8.2–§8.3, every verb route
(start / stop / snapshot / migrate) needs the same three primitives:

1. **Node resolver** — given an endpoint and a Proxmox ``vmid``, find
   the node hosting the VM via ``cluster/resources``.
2. **NetBox VM resolver** — given a NetBox API and a ``vmid``, find the
   linked NetBox ``VirtualMachine`` by its ``proxmox_vm_id`` custom
   field so the journal entry can be anchored to it.
3. **Journal-entry writer** — POST to ``/api/extras/journal-entries/``
   with the structured Markdown payload from §6.1.

The functions return structured ``JSONResponse`` errors (or values)
rather than raise, so the route handler can compose them with the
existing 403 gate path in ``proxbox_api/routes/proxmox_actions.py``
without restructuring exception flow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import status
from fastapi.responses import JSONResponse

from proxbox_api.exception import ProxboxException, ProxmoxAPIError
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_create_async, rest_first_async
from proxbox_api.services.proxmox_helpers import get_cluster_resources

Verb = Literal["start", "stop", "snapshot", "migrate"]
VmType = Literal["qemu", "lxc"]
JournalKind = Literal["info", "success", "warning", "danger"]


async def resolve_proxmox_node(
    session: object,
    vm_type: str,
    vmid: int,
) -> str | JSONResponse:
    """Return the Proxmox node hosting ``vmid``, or a 404 ``JSONResponse``.

    The route handler uses this to look up the node before dispatching a
    POST to ``nodes/{node}/{vm_type}/{vmid}/status/...``. We deliberately
    do not cache: cluster topology changes (migrate, failover) are rare
    but real, and the verb path is operator-initiated so an extra
    ``cluster/resources`` call is acceptable.
    """
    try:
        resources = await get_cluster_resources(session, resource_type=vm_type)
    except ProxmoxAPIError as error:
        logger.warning("Failed to fetch cluster resources for %s/%s: %s", vm_type, vmid, error)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "reason": "proxmox_cluster_resources_unreachable",
                "detail": (
                    "Unable to fetch cluster resources from Proxmox to resolve the "
                    f"node hosting {vm_type}/{vmid}."
                ),
            },
        )

    for item in resources:
        if item.vmid == vmid and item.node:
            return item.node

    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "reason": "vm_not_found_in_cluster",
            "detail": (
                f"No {vm_type} resource with vmid={vmid} found in cluster/resources. "
                "The VM may not exist or may not yet be reflected in the cluster."
            ),
            "vm_type": vm_type,
            "vmid": vmid,
        },
    )


async def resolve_netbox_vm_id(nb: object, vmid: int) -> int | None:
    """Return the NetBox ``VirtualMachine.id`` linked to a Proxmox ``vmid``.

    Returns ``None`` if no NetBox VM carries the matching
    ``proxmox_vm_id`` custom field. The verb route still proceeds — the
    journal entry simply cannot be anchored — and surfaces this case in
    the response so the operator can investigate.
    """
    try:
        record = await rest_first_async(
            nb,
            "/api/virtualization/virtual-machines/",
            query={"cf_proxmox_vm_id": vmid, "limit": 2},
        )
    except ProxboxException:
        raise
    except Exception as error:  # noqa: BLE001
        logger.warning("Failed to resolve NetBox VM for vmid=%s: %s", vmid, error)
        return None

    if record is None:
        return None
    netbox_id = record.get("id") if isinstance(record, dict) else None
    if netbox_id is None and hasattr(record, "__getitem__"):
        try:
            netbox_id = record["id"]
        except (KeyError, TypeError):
            netbox_id = None
    return int(netbox_id) if netbox_id is not None else None


def build_journal_comments(
    *,
    verb: str,
    actor: str,
    result: str,
    endpoint_name: str,
    endpoint_id: int,
    dispatched_at: str,
    proxmox_task_upid: str | None = None,
    idempotency_key: str | None = None,
    error_detail: str | None = None,
) -> str:
    """Build the structured-Markdown journal ``comments`` body (§6.1)."""
    lines = [
        "Proxbox operational verb dispatched.",
        "",
        f"- verb: {verb}",
        f"- actor: {actor}",
        f"- result: {result}",
    ]
    if proxmox_task_upid is not None:
        lines.append(f"- proxmox_task_upid: {proxmox_task_upid}")
    if idempotency_key is not None:
        lines.append(f"- idempotency_key: {idempotency_key}")
    lines.append(f"- endpoint: {endpoint_name} (id={endpoint_id})")
    lines.append(f"- dispatched_at: {dispatched_at}")
    if error_detail is not None:
        lines.append(f"- error_detail: {error_detail}")
    return "\n".join(lines)


async def write_verb_journal_entry(
    nb: object,
    *,
    netbox_vm_id: int,
    kind: JournalKind,
    comments: str,
) -> dict[str, object] | None:
    """POST a verb-dispatch journal entry to NetBox. Returns the entry or ``None`` on failure."""
    try:
        record = await rest_create_async(
            nb,
            "/api/extras/journal-entries/",
            {
                "assigned_object_type": "virtualization.virtualmachine",
                "assigned_object_id": netbox_vm_id,
                "kind": kind,
                "comments": comments,
            },
        )
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "Failed to write verb journal entry for netbox_vm_id=%s: %s",
            netbox_vm_id,
            error,
        )
        return None
    body = getattr(record, "data", None)
    if isinstance(body, dict):
        return body
    if isinstance(record, dict):
        return record
    return None


def utcnow_iso() -> str:
    """ISO-8601 timestamp in UTC, second precision (``2026-05-12T18:42:11Z``)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_success_response(
    *,
    verb: str,
    vm_type: str,
    vmid: int,
    endpoint_id: int,
    result: str,
    dispatched_at: str,
    proxmox_task_upid: str | None = None,
    journal_entry_url: str | None = None,
) -> dict[str, object]:
    """Construct the §7.3 non-migrate success response shape."""
    body: dict[str, object] = {
        "verb": verb,
        "vmid": vmid,
        "vm_type": vm_type,
        "endpoint_id": endpoint_id,
        "result": result,
        "dispatched_at": dispatched_at,
    }
    if proxmox_task_upid is not None:
        body["proxmox_task_upid"] = proxmox_task_upid
    if journal_entry_url is not None:
        body["journal_entry_url"] = journal_entry_url
    return body

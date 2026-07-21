"""Shared infrastructure for operational verb routes (issue #376).

Per ``docs/design/operational-verbs.md`` §8.2–§8.3, every verb route
needs the same three primitives:

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

from proxbox_api.exception import NetBoxAPIError, ProxboxException, ProxmoxAPIError
from proxbox_api.logger import logger
from proxbox_api.netbox_rest import rest_create_async
from proxbox_api.services.proxmox_helpers import get_cluster_resources
from proxbox_api.services.sync.sync_state_reader import (
    resolve_virtual_machine_by_sync_state,
)
from proxbox_api.utils.log_scrubbing import scrub_cloud_init

Verb = Literal[
    "start",
    "stop",
    "snapshot",
    "migrate",
    "reboot",
    "delete",
    "backup",
    "delete_snapshot",
]
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


async def resolve_netbox_vm_id(
    nb: object,
    vmid: int,
    *,
    endpoint_id: int | None = None,
    cluster_id: int | None = None,
    fail_closed: bool = False,
) -> int | None:
    """Return the scoped NetBox ``VirtualMachine.id`` linked to a Proxmox VMID.

    ``endpoint_id`` is the proxbox-api ``ProxmoxEndpoint`` id mirrored into
    VM sync-state sidecars and legacy custom fields. When ``fail_closed`` is
    true, ambiguous, unverifiable, or absent identities raise before the caller
    can dispatch a Proxmox write that cannot be durably journaled.
    """
    try:
        resolution = await resolve_virtual_machine_by_sync_state(
            nb,
            proxmox_vm_id=vmid,
            endpoint_id=endpoint_id,
            cluster_id=cluster_id,
            fail_on_ambiguous=fail_closed,
        )
    except ProxboxException as error:
        if fail_closed:
            raise ProxboxException(
                message="Refusing to dispatch operational verb without a verifiable NetBox VM audit target.",
                detail={
                    "reason": "netbox_vm_identity_unverifiable_for_audit",
                    "vmid": vmid,
                    "endpoint_id": endpoint_id,
                    "cluster_id": cluster_id,
                    "resolver_detail": error.detail or error.message,
                },
                http_status_code=status.HTTP_409_CONFLICT,
            ) from error
        raise
    except Exception as error:  # noqa: BLE001
        logger.warning("Failed to resolve NetBox VM for vmid=%s: %s", vmid, error)
        if fail_closed:
            raise ProxboxException(
                message="Refusing to dispatch operational verb without a verifiable NetBox VM audit target.",
                detail={
                    "reason": "netbox_vm_identity_unverifiable_for_audit",
                    "vmid": vmid,
                    "endpoint_id": endpoint_id,
                    "cluster_id": cluster_id,
                },
                http_status_code=status.HTTP_409_CONFLICT,
            ) from error
        return None

    if resolution is None:
        if fail_closed:
            raise ProxboxException(
                message="Refusing to dispatch operational verb without a NetBox VM audit target.",
                detail={
                    "reason": "netbox_vm_identity_required_for_audit",
                    "vmid": vmid,
                    "endpoint_id": endpoint_id,
                    "cluster_id": cluster_id,
                },
                http_status_code=status.HTTP_409_CONFLICT,
            )
        return None
    return resolution.record_id


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
    """POST a verb-dispatch journal entry to NetBox.

    Raises on write failure so operational verbs fail closed instead of
    succeeding without a durable audit record.
    """
    payload = scrub_cloud_init(
        {
            "assigned_object_type": "virtualization.virtualmachine",
            "assigned_object_id": netbox_vm_id,
            "kind": kind,
            "comments": comments,
        }
    )
    try:
        record = await rest_create_async(
            nb,
            "/api/extras/journal-entries/",
            payload,
        )
    except ProxboxException as error:
        logger.warning(
            "Failed to write verb journal entry for netbox_vm_id=%s: %s",
            netbox_vm_id,
            error,
        )
        raise
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "Failed to write verb journal entry for netbox_vm_id=%s: %s",
            netbox_vm_id,
            error,
        )
        raise NetBoxAPIError(
            "Failed to write verb audit journal entry",
            endpoint="/api/extras/journal-entries/",
            method="POST",
            original_error=error,
        ) from error
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

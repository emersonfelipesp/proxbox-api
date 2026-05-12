"""Operational verb routes (start / stop / snapshot / migrate).

Issue #376. Sub-PR B introduced the gate stub; sub-PR C wired the
``start`` verb; sub-PR D wires ``stop``; sub-PR E wires ``snapshot``;
sub-PR F wires ``migrate`` (the only async verb) plus its cancel and
SSE-stream endpoints.
The 403 ``allow_writes`` gate at the top of every handler is the
load-bearing trust boundary described in ``operational-verbs.md`` §2.3
layer 3 — it must remain in place after every verb is wired.

Each verb obeys the contract pinned in ``docs/design/operational-verbs.md``:

- **§4 Idempotency.** Optional ``Idempotency-Key`` HTTP header. Within
  a 60-second window, a second POST with the same key for the same
  ``(endpoint_id, verb, vmid)`` returns the cached response without
  re-dispatching to Proxmox.
- **§4.2 State-based no-op.** The route calls ``get_vm_status`` before
  dispatch; if the target state already holds (e.g. ``start`` against
  a ``running`` VM), the route returns ``result: "already_running"``
  with no Proxmox call. No-ops still write a journal entry.
- **§6 Audit.** Every invocation — success, failure, or no-op — writes
  exactly one journal entry on the linked NetBox ``VirtualMachine``
  (resolved by the ``proxmox_vm_id`` custom field). Even a Proxmox 500
  writes a ``kind: "warning"`` entry; failure to audit is a P0 bug.
- **§7.3 Response shape.** ``verb``, ``vmid``, ``vm_type``,
  ``endpoint_id``, ``result``, ``dispatched_at`` and, on real
  dispatch, ``proxmox_task_upid`` + ``journal_entry_url``.

The route handlers accept an optional ``endpoint_id`` query parameter so
callers can target a specific Proxmox cluster among many. When omitted,
the gate returns ``reason: "endpoint_id_required"``. The plugin will
always pass it once the backend-proxy view is wired in sub-PR G.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import AsyncGenerator, Literal

from fastapi import APIRouter, Body, Header, Query, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.exception import ProxboxException, ProxmoxAPIError
from proxbox_api.logger import logger
from proxbox_api.session.netbox import get_netbox_async_session
from proxbox_api.session.proxmox import ProxmoxSession
from proxbox_api.session.proxmox_providers import _parse_db_endpoint
from proxbox_api.services.idempotency import CacheKey, get_idempotency_cache
from proxbox_api.services.proxmox_helpers import (
    cancel_task,
    create_vm_snapshot,
    get_node_task_status,
    get_vm_status,
    migrate_preflight,
    migrate_vm,
    start_vm,
    stop_vm,
)
from proxbox_api.services.verb_dispatch import (
    build_journal_comments,
    build_success_response,
    resolve_netbox_vm_id,
    resolve_proxmox_node,
    utcnow_iso,
    write_verb_journal_entry,
)
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

router = APIRouter()

VmType = Literal["qemu", "lxc"]
Verb = Literal["start", "stop", "snapshot", "migrate"]


async def _gate(
    session: SessionDep, endpoint_id: int | None
) -> JSONResponse | ProxmoxEndpoint:
    """Resolve the target endpoint and enforce ``allow_writes``."""
    if endpoint_id is None:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "reason": "endpoint_id_required",
                "detail": (
                    "Operational verbs require an explicit endpoint_id query "
                    "parameter so the gate can resolve the target Proxmox cluster."
                ),
            },
        )

    endpoint = await _maybe_await(session.get(ProxmoxEndpoint, endpoint_id))
    if endpoint is None:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "reason": "endpoint_not_found",
                "detail": f"No ProxmoxEndpoint with id={endpoint_id}.",
            },
        )

    if not endpoint.allow_writes:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "reason": "endpoint_writes_disabled",
                "detail": (
                    "Operational verbs are disabled on this endpoint. Enable "
                    "ProxmoxEndpoint.allow_writes on the NetBox side after "
                    "granting core.run_proxmox_action to the operator group."
                ),
                "endpoint_id": endpoint.id,
            },
        )

    return endpoint


async def _open_proxmox_session(endpoint: ProxmoxEndpoint) -> ProxmoxSession:
    """Open a Proxmox API session for ``endpoint`` (factored for testability)."""
    schema = _parse_db_endpoint(endpoint)
    return await ProxmoxSession.create(schema)


def _is_stopped(vm_type: str, status_payload: object) -> bool:
    """True when the VM's current Proxmox ``status`` is ``"stopped"``.

    Works for both QEMU and LXC response schemas. Treats missing/None
    as "not stopped" so the verb proceeds to dispatch and surfaces any
    failure there.
    """
    value = getattr(status_payload, "status", None)
    return value == "stopped"


def _is_running(vm_type: str, status_payload: object) -> bool:
    """True when the VM's current Proxmox ``status`` is ``"running"``.

    Works for both QEMU and LXC response schemas (both expose
    ``status: str``). Treats missing/None as "not running" so the verb
    proceeds to dispatch and surfaces any failure there.
    """
    value = getattr(status_payload, "status", None)
    return value == "running"


async def _dispatch_start(
    *,
    endpoint: ProxmoxEndpoint,
    vm_type: VmType,
    vmid: int,
    nb: object,
    idempotency_key: str | None,
    actor: str,
) -> JSONResponse:
    """Execute the start verb: resolve node, pre-flight, dispatch, audit.

    Sub-PR C entry point. Stop/snapshot/migrate (D–F) follow the same
    skeleton, swapping ``start_vm`` for the verb-specific helper and
    adding any verb-specific pre-flight (migrate has the most).
    """
    endpoint_id = endpoint.id
    assert endpoint_id is not None  # ProxmoxEndpoint.id is PK; cannot be None on a fetched row

    cache = get_idempotency_cache()
    cache_key: CacheKey | None = None
    if idempotency_key:
        cache_key = CacheKey(
            endpoint_id=endpoint_id, verb="start", vmid=vmid, key=idempotency_key
        )
        cached = await cache.get(cache_key)
        if cached is not None:
            return JSONResponse(status_code=status.HTTP_200_OK, content=cached)

    try:
        proxmox = await _open_proxmox_session(endpoint)
    except ProxboxException as error:
        logger.warning("Failed to open Proxmox session for endpoint=%s: %s", endpoint_id, error)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "reason": "proxmox_session_unreachable",
                "detail": str(error),
                "endpoint_id": endpoint_id,
            },
        )

    node_or_error = await resolve_proxmox_node(proxmox, vm_type, vmid)
    if isinstance(node_or_error, JSONResponse):
        return node_or_error
    node: str = node_or_error

    netbox_vm_id = await resolve_netbox_vm_id(nb, vmid)
    dispatched_at = utcnow_iso()

    # State-based no-op pre-flight (§4.2). Reached before any cache write.
    try:
        current = await get_vm_status(proxmox, node, vm_type, vmid)
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            verb="start",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            result="failed",
            kind="warning",
            proxmox_task_upid=None,
            error_detail=str(error),
            http_status=status.HTTP_502_BAD_GATEWAY,
            reason="proxmox_status_unreachable",
        )

    if _is_running(vm_type, current):
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            verb="start",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            result="already_running",
            kind="info",
            proxmox_task_upid=None,
            error_detail=None,
        )

    # Dispatch the verb.
    try:
        upid = await start_vm(proxmox, node, vm_type, vmid)
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            verb="start",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            result="failed",
            kind="warning",
            proxmox_task_upid=None,
            error_detail=str(error),
            http_status=status.HTTP_502_BAD_GATEWAY,
            reason="proxmox_dispatch_failed",
        )

    return await _audit_and_respond(
        nb=nb,
        netbox_vm_id=netbox_vm_id,
        verb="start",
        vm_type=vm_type,
        vmid=vmid,
        endpoint=endpoint,
        actor=actor,
        dispatched_at=dispatched_at,
        idempotency_key=idempotency_key,
        cache=cache,
        cache_key=cache_key,
        result="ok",
        kind="info",
        proxmox_task_upid=upid,
        error_detail=None,
    )


async def _dispatch_stop(
    *,
    endpoint: ProxmoxEndpoint,
    vm_type: VmType,
    vmid: int,
    nb: object,
    idempotency_key: str | None,
    actor: str,
) -> JSONResponse:
    """Execute the stop verb: resolve node, pre-flight, dispatch, audit.

    Mirrors ``_dispatch_start``; the only changes are the no-op state
    (``status == "stopped"`` → ``already_stopped``) and the Proxmox
    POST target (``status/stop`` instead of ``status/start``).
    """
    endpoint_id = endpoint.id
    assert endpoint_id is not None

    cache = get_idempotency_cache()
    cache_key: CacheKey | None = None
    if idempotency_key:
        cache_key = CacheKey(
            endpoint_id=endpoint_id, verb="stop", vmid=vmid, key=idempotency_key
        )
        cached = await cache.get(cache_key)
        if cached is not None:
            return JSONResponse(status_code=status.HTTP_200_OK, content=cached)

    try:
        proxmox = await _open_proxmox_session(endpoint)
    except ProxboxException as error:
        logger.warning("Failed to open Proxmox session for endpoint=%s: %s", endpoint_id, error)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "reason": "proxmox_session_unreachable",
                "detail": str(error),
                "endpoint_id": endpoint_id,
            },
        )

    node_or_error = await resolve_proxmox_node(proxmox, vm_type, vmid)
    if isinstance(node_or_error, JSONResponse):
        return node_or_error
    node: str = node_or_error

    netbox_vm_id = await resolve_netbox_vm_id(nb, vmid)
    dispatched_at = utcnow_iso()

    try:
        current = await get_vm_status(proxmox, node, vm_type, vmid)
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            verb="stop",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            result="failed",
            kind="warning",
            proxmox_task_upid=None,
            error_detail=str(error),
            http_status=status.HTTP_502_BAD_GATEWAY,
            reason="proxmox_status_unreachable",
        )

    if _is_stopped(vm_type, current):
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            verb="stop",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            result="already_stopped",
            kind="info",
            proxmox_task_upid=None,
            error_detail=None,
        )

    try:
        upid = await stop_vm(proxmox, node, vm_type, vmid)
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            verb="stop",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            result="failed",
            kind="warning",
            proxmox_task_upid=None,
            error_detail=str(error),
            http_status=status.HTTP_502_BAD_GATEWAY,
            reason="proxmox_dispatch_failed",
        )

    return await _audit_and_respond(
        nb=nb,
        netbox_vm_id=netbox_vm_id,
        verb="stop",
        vm_type=vm_type,
        vmid=vmid,
        endpoint=endpoint,
        actor=actor,
        dispatched_at=dispatched_at,
        idempotency_key=idempotency_key,
        cache=cache,
        cache_key=cache_key,
        result="ok",
        kind="info",
        proxmox_task_upid=upid,
        error_detail=None,
    )


class SnapshotRequest(BaseModel):
    """Optional request body for the snapshot verb.

    Both fields are optional. When ``snapname`` is omitted, the route
    generates a deterministic default (``proxbox-{idempotency_key[:8]}``
    if an ``Idempotency-Key`` is supplied, else ``proxbox-{utc-stamp}``)
    per ``operational-verbs.md`` §13.
    """

    snapname: str | None = None
    description: str | None = None


def _default_snapname(idempotency_key: str | None) -> str:
    """Generate a default snapshot name per §13.

    Proxmox snapshot names must match ``[A-Za-z][A-Za-z0-9_-]*``, so the
    UTC timestamp fallback uses a compact form free of ``:``/``.``.
    """
    if idempotency_key:
        return f"proxbox-{idempotency_key[:8]}"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"proxbox-{stamp}"


async def _dispatch_snapshot(
    *,
    endpoint: ProxmoxEndpoint,
    vm_type: VmType,
    vmid: int,
    nb: object,
    idempotency_key: str | None,
    actor: str,
    snapname: str | None,
    description: str | None,
) -> JSONResponse:
    """Execute the snapshot verb: resolve node, dispatch, audit.

    Snapshot is "always dispatched" (§4.2): no state-based no-op
    pre-flight. The operator initiating the click is assumed to know
    they are creating a new snapshot. Idempotency-Key (§4) still
    deduplicates two clicks within the cache window.
    """
    endpoint_id = endpoint.id
    assert endpoint_id is not None

    cache = get_idempotency_cache()
    cache_key: CacheKey | None = None
    if idempotency_key:
        cache_key = CacheKey(
            endpoint_id=endpoint_id, verb="snapshot", vmid=vmid, key=idempotency_key
        )
        cached = await cache.get(cache_key)
        if cached is not None:
            return JSONResponse(status_code=status.HTTP_200_OK, content=cached)

    try:
        proxmox = await _open_proxmox_session(endpoint)
    except ProxboxException as error:
        logger.warning("Failed to open Proxmox session for endpoint=%s: %s", endpoint_id, error)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "reason": "proxmox_session_unreachable",
                "detail": str(error),
                "endpoint_id": endpoint_id,
            },
        )

    node_or_error = await resolve_proxmox_node(proxmox, vm_type, vmid)
    if isinstance(node_or_error, JSONResponse):
        return node_or_error
    node: str = node_or_error

    netbox_vm_id = await resolve_netbox_vm_id(nb, vmid)
    dispatched_at = utcnow_iso()

    effective_snapname = snapname or _default_snapname(idempotency_key)

    try:
        upid = await create_vm_snapshot(
            proxmox, node, vm_type, vmid, effective_snapname, description
        )
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            verb="snapshot",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            result="failed",
            kind="warning",
            proxmox_task_upid=None,
            error_detail=str(error),
            http_status=status.HTTP_502_BAD_GATEWAY,
            reason="proxmox_dispatch_failed",
            extra={"snapname": effective_snapname},
        )

    return await _audit_and_respond(
        nb=nb,
        netbox_vm_id=netbox_vm_id,
        verb="snapshot",
        vm_type=vm_type,
        vmid=vmid,
        endpoint=endpoint,
        actor=actor,
        dispatched_at=dispatched_at,
        idempotency_key=idempotency_key,
        cache=cache,
        cache_key=cache_key,
        result="ok",
        kind="info",
        proxmox_task_upid=upid,
        error_detail=None,
        extra={"snapname": effective_snapname},
    )


class MigrateRequest(BaseModel):
    """Request body for the migrate verb (§9).

    ``target`` is required, but validated inside the handler **after**
    the §2.3 gate so a write-disabled endpoint still returns 403 (not
    422) when the body is missing or incomplete. ``online`` enables live
    migration for QEMU and equivalent restart-at-target behaviour for
    LXC (Proxmox uses a different parameter name there). When ``online``
    is ``True``, the pre-flight rejects if the VM has local-only disks
    or resources.
    """

    target: str | None = None
    online: bool = False


def _migrate_sse_url(vm_type: VmType, vmid: int, task_upid: str) -> str:
    return f"/proxmox/{vm_type}/{vmid}/migrate/{task_upid}/stream"


def _preflight_rejection(
    preflight: dict[str, object], target: str, online: bool
) -> tuple[str, str] | None:
    """Return ``(reason, detail)`` if the preflight should reject, else None.

    Encodes the §9 reject conditions:
      - ``target`` not in ``allowed_nodes`` → ``target_not_allowed``
      - online + non-empty ``local_disks`` → ``local_disks_block_online_migrate``
      - online + non-empty ``local_resources`` → ``local_resources_block_online_migrate``
    """
    allowed = preflight.get("allowed_nodes") or []
    if isinstance(allowed, list) and target not in allowed:
        return (
            "target_not_allowed",
            f"target {target!r} is not in allowed_nodes={list(allowed)!r}",
        )
    if online:
        local_disks = preflight.get("local_disks") or []
        if isinstance(local_disks, list) and local_disks:
            return (
                "local_disks_block_online_migrate",
                "Online migration is blocked by local-only disks on the source node.",
            )
        local_resources = preflight.get("local_resources") or []
        if isinstance(local_resources, list) and local_resources:
            return (
                "local_resources_block_online_migrate",
                "Online migration is blocked by local resources on the source node.",
            )
    return None


async def _dispatch_migrate(
    *,
    endpoint: ProxmoxEndpoint,
    vm_type: VmType,
    vmid: int,
    nb: object,
    idempotency_key: str | None,
    actor: str,
    target: str,
    online: bool,
) -> JSONResponse:
    """Execute the migrate verb: preflight (§9), dispatch, journal, 202.

    Unlike start/stop/snapshot, migrate is **async**: the POST returns
    202 with ``proxmox_task_upid`` + ``sse_url`` so the caller can open
    the stream endpoint. The dispatch journal entry is written here at
    202 time; the SSE stream emits the final-state journal entry when
    the task completes.
    """
    endpoint_id = endpoint.id
    assert endpoint_id is not None

    cache = get_idempotency_cache()
    cache_key: CacheKey | None = None
    if idempotency_key:
        cache_key = CacheKey(
            endpoint_id=endpoint_id, verb="migrate", vmid=vmid, key=idempotency_key
        )
        cached = await cache.get(cache_key)
        if cached is not None:
            return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=cached)

    try:
        proxmox = await _open_proxmox_session(endpoint)
    except ProxboxException as error:
        logger.warning("Failed to open Proxmox session for endpoint=%s: %s", endpoint_id, error)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "reason": "proxmox_session_unreachable",
                "detail": str(error),
                "endpoint_id": endpoint_id,
            },
        )

    node_or_error = await resolve_proxmox_node(proxmox, vm_type, vmid)
    if isinstance(node_or_error, JSONResponse):
        return node_or_error
    node: str = node_or_error

    netbox_vm_id = await resolve_netbox_vm_id(nb, vmid)
    dispatched_at = utcnow_iso()

    # §9 preflight: GET nodes/{node}/{vm_type}/{vmid}/migrate, then
    # apply the three reject conditions before any state mutation.
    try:
        preflight = await migrate_preflight(proxmox, node, vm_type, vmid)
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            verb="migrate",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            result="failed",
            kind="warning",
            proxmox_task_upid=None,
            error_detail=str(error),
            http_status=status.HTTP_502_BAD_GATEWAY,
            reason="proxmox_preflight_failed",
        )

    rejection = _preflight_rejection(preflight, target, online)
    if rejection is not None:
        reason, detail = rejection
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            verb="migrate",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            result="rejected",
            kind="warning",
            proxmox_task_upid=None,
            error_detail=detail,
            http_status=status.HTTP_400_BAD_REQUEST,
            reason=reason,
            extra={"preflight": preflight, "target": target, "online": online},
        )

    try:
        upid = await migrate_vm(proxmox, node, vm_type, vmid, target, online)
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            verb="migrate",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            result="failed",
            kind="warning",
            proxmox_task_upid=None,
            error_detail=str(error),
            http_status=status.HTTP_502_BAD_GATEWAY,
            reason="proxmox_dispatch_failed",
        )

    return await _audit_and_respond(
        nb=nb,
        netbox_vm_id=netbox_vm_id,
        verb="migrate",
        vm_type=vm_type,
        vmid=vmid,
        endpoint=endpoint,
        actor=actor,
        dispatched_at=dispatched_at,
        idempotency_key=idempotency_key,
        cache=cache,
        cache_key=cache_key,
        result="accepted",
        kind="info",
        proxmox_task_upid=upid,
        error_detail=None,
        http_status=status.HTTP_202_ACCEPTED,
        extra={
            "sse_url": _migrate_sse_url(vm_type, vmid, upid),
            "target": target,
            "online": online,
            "source_node": node,
        },
    )


async def _audit_and_respond(
    *,
    nb: object,
    netbox_vm_id: int | None,
    verb: Verb,
    vm_type: VmType,
    vmid: int,
    endpoint: ProxmoxEndpoint,
    actor: str,
    dispatched_at: str,
    idempotency_key: str | None,
    cache: object,
    cache_key: CacheKey | None,
    result: str,
    kind: Literal["info", "success", "warning", "danger"],
    proxmox_task_upid: str | None,
    error_detail: str | None,
    http_status: int = status.HTTP_200_OK,
    reason: str | None = None,
    extra: dict[str, object] | None = None,
) -> JSONResponse:
    """Write the journal entry, cache the response, return JSONResponse.

    Centralises the §6 + §7.3 + §4 cache contracts so the dispatch flow
    above stays readable. ``http_status``/``reason`` are passed only on
    error paths; the success / no-op paths use the §7.3 shape verbatim.
    ``extra`` carries verb-specific fields (e.g. snapshot's ``snapname``)
    that the §7.3 base shape doesn't model.
    """
    comments = build_journal_comments(
        verb=verb,
        actor=actor,
        result=result,
        endpoint_name=endpoint.name,
        endpoint_id=endpoint.id or 0,
        dispatched_at=dispatched_at,
        proxmox_task_upid=proxmox_task_upid,
        idempotency_key=idempotency_key,
        error_detail=error_detail,
    )

    journal_entry_url: str | None = None
    if netbox_vm_id is not None:
        entry = await write_verb_journal_entry(
            nb, netbox_vm_id=netbox_vm_id, kind=kind, comments=comments
        )
        if entry is not None:
            url = entry.get("url") if isinstance(entry, dict) else None
            entry_id = entry.get("id") if isinstance(entry, dict) else None
            if isinstance(url, str):
                journal_entry_url = url
            elif isinstance(entry_id, int):
                journal_entry_url = f"/api/extras/journal-entries/{entry_id}/"

    body = build_success_response(
        verb=verb,
        vm_type=vm_type,
        vmid=vmid,
        endpoint_id=endpoint.id or 0,
        result=result,
        dispatched_at=dispatched_at,
        proxmox_task_upid=proxmox_task_upid,
        journal_entry_url=journal_entry_url,
    )
    if reason is not None:
        body["reason"] = reason
    if error_detail is not None and http_status >= 400:
        body["detail"] = error_detail
    if extra:
        for key, value in extra.items():
            body.setdefault(key, value)

    # Cache only the body, not the HTTP status — but only when the
    # caller supplied an Idempotency-Key. The §4 contract reuses the
    # full response on the second call. Both 200 (start/stop/snapshot)
    # and 202 (migrate dispatch) are cacheable success states.
    if cache_key is not None and http_status in (
        status.HTTP_200_OK,
        status.HTTP_202_ACCEPTED,
    ):
        await cache.store(cache_key, body)  # type: ignore[attr-defined]

    return JSONResponse(status_code=http_status, content=body)


def _not_implemented(verb: Verb, vm_type: VmType, vmid: int) -> JSONResponse:
    """Sub-PR B stub placeholder for verbs not yet wired (D–F)."""
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={
            "reason": "verb_not_yet_implemented",
            "detail": (
                f"The {verb!r} verb for {vm_type!r}/{vmid} is gated open but the "
                "dispatch path lands in a follow-up sub-PR (#376 D–F)."
            ),
            "verb": verb,
            "vm_type": vm_type,
            "vmid": vmid,
        },
    )


async def _handle_start(
    vm_type: VmType,
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None,
    idempotency_key: str | None,
    actor: str,
) -> JSONResponse:
    # Gate first; the 403 path must NOT depend on NetBox being reachable.
    gated = await _gate(session, endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated
    # Only resolve the NetBox session once the gate is open. This keeps
    # the §2.3 layer-3 trust boundary independent of NetBox availability.
    try:
        nb_session = await get_netbox_async_session(database_session=session)
    except ProxboxException as error:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "reason": "netbox_session_unavailable",
                "detail": str(error),
            },
        )
    return await _dispatch_start(
        endpoint=gated,
        vm_type=vm_type,
        vmid=vmid,
        nb=nb_session,
        idempotency_key=idempotency_key,
        actor=actor,
    )


async def _handle_stop(
    vm_type: VmType,
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None,
    idempotency_key: str | None,
    actor: str,
) -> JSONResponse:
    gated = await _gate(session, endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated
    try:
        nb_session = await get_netbox_async_session(database_session=session)
    except ProxboxException as error:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "reason": "netbox_session_unavailable",
                "detail": str(error),
            },
        )
    return await _dispatch_stop(
        endpoint=gated,
        vm_type=vm_type,
        vmid=vmid,
        nb=nb_session,
        idempotency_key=idempotency_key,
        actor=actor,
    )


async def _handle_snapshot(
    vm_type: VmType,
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None,
    idempotency_key: str | None,
    actor: str,
    body: SnapshotRequest | None,
) -> JSONResponse:
    gated = await _gate(session, endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated
    try:
        nb_session = await get_netbox_async_session(database_session=session)
    except ProxboxException as error:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "reason": "netbox_session_unavailable",
                "detail": str(error),
            },
        )
    snapname = body.snapname if body is not None else None
    description = body.description if body is not None else None
    return await _dispatch_snapshot(
        endpoint=gated,
        vm_type=vm_type,
        vmid=vmid,
        nb=nb_session,
        idempotency_key=idempotency_key,
        actor=actor,
        snapname=snapname,
        description=description,
    )


async def _handle_migrate(
    vm_type: VmType,
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None,
    idempotency_key: str | None,
    actor: str,
    body: MigrateRequest | None,
) -> JSONResponse:
    gated = await _gate(session, endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated
    # ``target`` is required, but validated **after** the gate so the
    # 403 trust boundary doesn't leak the body schema to unauthorised
    # callers.
    if body is None or not body.target:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "reason": "target_required",
                "detail": "Migrate verb requires a JSON body with a non-empty 'target' field.",
            },
        )
    try:
        nb_session = await get_netbox_async_session(database_session=session)
    except ProxboxException as error:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "reason": "netbox_session_unavailable",
                "detail": str(error),
            },
        )
    return await _dispatch_migrate(
        endpoint=gated,
        vm_type=vm_type,
        vmid=vmid,
        nb=nb_session,
        idempotency_key=idempotency_key,
        actor=actor,
        target=body.target,
        online=body.online,
    )


async def _handle_migrate_cancel(
    vm_type: VmType,
    vmid: int,
    task_upid: str,
    session: SessionDep,
    endpoint_id: int | None,
    actor: str,
) -> JSONResponse:
    """Cancel an in-flight migrate task (§5).

    Best-effort: Proxmox decides whether the task can be torn down. We
    write a journal entry on every cancel attempt so the operator-side
    audit trail records the intent even if the task already completed.
    """
    gated = await _gate(session, endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated
    try:
        nb_session = await get_netbox_async_session(database_session=session)
    except ProxboxException as error:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "reason": "netbox_session_unavailable",
                "detail": str(error),
            },
        )
    endpoint = gated
    try:
        proxmox = await _open_proxmox_session(endpoint)
    except ProxboxException as error:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "reason": "proxmox_session_unreachable",
                "detail": str(error),
                "endpoint_id": endpoint.id,
            },
        )

    node_or_error = await resolve_proxmox_node(proxmox, vm_type, vmid)
    if isinstance(node_or_error, JSONResponse):
        return node_or_error
    node: str = node_or_error

    netbox_vm_id = await resolve_netbox_vm_id(nb_session, vmid)
    dispatched_at = utcnow_iso()

    try:
        await cancel_task(proxmox, node, task_upid)
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb_session,
            netbox_vm_id=netbox_vm_id,
            verb="migrate",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=None,
            cache=get_idempotency_cache(),
            cache_key=None,
            result="cancel_failed",
            kind="warning",
            proxmox_task_upid=task_upid,
            error_detail=str(error),
            http_status=status.HTTP_502_BAD_GATEWAY,
            reason="proxmox_cancel_failed",
        )

    return await _audit_and_respond(
        nb=nb_session,
        netbox_vm_id=netbox_vm_id,
        verb="migrate",
        vm_type=vm_type,
        vmid=vmid,
        endpoint=endpoint,
        actor=actor,
        dispatched_at=dispatched_at,
        idempotency_key=None,
        cache=get_idempotency_cache(),
        cache_key=None,
        result="cancel_requested",
        kind="info",
        proxmox_task_upid=task_upid,
        error_detail=None,
    )


async def _migrate_stream_generator(
    *,
    proxmox: ProxmoxSession,
    node: str,
    task_upid: str,
    vm_type: VmType,
    vmid: int,
    endpoint_id: int,
    poll_interval: float = 2.0,
    keepalive_interval: float = 15.0,
) -> AsyncGenerator[str, None]:
    """Yield SSE frames covering the migrate task lifecycle (§7.1).

    Emits, in order:
      1. ``migrate_dispatched`` — once, immediately.
      2. ``migrate_progress`` — repeating while the task is running.
      3. ``migrate_succeeded`` xor ``migrate_failed`` — final frame
         based on the Proxmox ``exitstatus``.

    A keepalive comment is interleaved between polls to keep proxies
    from closing the connection.
    """
    dispatched_frame = {
        "event": "migrate_dispatched",
        "data": {
            "verb": "migrate",
            "vm_type": vm_type,
            "vmid": vmid,
            "endpoint_id": endpoint_id,
            "task_upid": task_upid,
            "node": node,
        },
    }
    yield f"event: {dispatched_frame['event']}\ndata: {json.dumps(dispatched_frame['data'])}\n\n"

    last_keepalive = asyncio.get_event_loop().time()
    while True:
        try:
            task_status = await get_node_task_status(proxmox, node, task_upid)
        except ProxmoxAPIError as error:
            failed = {
                "event": "migrate_failed",
                "data": {"task_upid": task_upid, "error_detail": str(error)},
            }
            yield f"event: {failed['event']}\ndata: {json.dumps(failed['data'])}\n\n"
            return

        status_field = getattr(task_status, "status", None) or (
            task_status.get("status") if isinstance(task_status, dict) else None
        )
        exitstatus = getattr(task_status, "exitstatus", None) or (
            task_status.get("exitstatus") if isinstance(task_status, dict) else None
        )

        if status_field == "stopped":
            ok = exitstatus == "OK" or exitstatus is None
            event_name = "migrate_succeeded" if ok else "migrate_failed"
            frame = {
                "event": event_name,
                "data": {
                    "task_upid": task_upid,
                    "exitstatus": exitstatus,
                },
            }
            yield f"event: {event_name}\ndata: {json.dumps(frame['data'])}\n\n"
            return

        progress_value = getattr(task_status, "progress", None)
        if progress_value is None and isinstance(task_status, dict):
            progress_value = task_status.get("progress")
        progress_frame = {
            "event": "migrate_progress",
            "data": {
                "task_upid": task_upid,
                "progress": progress_value,
                "status": status_field,
            },
        }
        yield f"event: {progress_frame['event']}\ndata: {json.dumps(progress_frame['data'])}\n\n"

        try:
            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            return

        now = asyncio.get_event_loop().time()
        if now - last_keepalive >= keepalive_interval:
            yield ": keepalive\n\n"
            last_keepalive = now


async def _handle_stub(
    verb: Verb,
    vm_type: VmType,
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None,
) -> JSONResponse:
    """Stub path for verbs not yet wired (sub-PRs D–F)."""
    gated = await _gate(session, endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated
    return _not_implemented(verb, vm_type, vmid)


def _actor_label(value: str | None) -> str:
    return value or "proxbox-api"


@router.post("/qemu/{vmid}/start")
async def start_qemu(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> JSONResponse:
    return await _handle_start(
        "qemu", vmid, session, endpoint_id, idempotency_key, _actor_label(actor)
    )


@router.post("/lxc/{vmid}/start")
async def start_lxc(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> JSONResponse:
    return await _handle_start(
        "lxc", vmid, session, endpoint_id, idempotency_key, _actor_label(actor)
    )


@router.post("/qemu/{vmid}/stop")
async def stop_qemu(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> JSONResponse:
    return await _handle_stop(
        "qemu", vmid, session, endpoint_id, idempotency_key, _actor_label(actor)
    )


@router.post("/lxc/{vmid}/stop")
async def stop_lxc(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> JSONResponse:
    return await _handle_stop(
        "lxc", vmid, session, endpoint_id, idempotency_key, _actor_label(actor)
    )


@router.post("/qemu/{vmid}/snapshot")
async def snapshot_qemu(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
    body: SnapshotRequest | None = Body(default=None),
) -> JSONResponse:
    return await _handle_snapshot(
        "qemu", vmid, session, endpoint_id, idempotency_key, _actor_label(actor), body
    )


@router.post("/lxc/{vmid}/snapshot")
async def snapshot_lxc(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
    body: SnapshotRequest | None = Body(default=None),
) -> JSONResponse:
    return await _handle_snapshot(
        "lxc", vmid, session, endpoint_id, idempotency_key, _actor_label(actor), body
    )


@router.post("/qemu/{vmid}/migrate")
async def migrate_qemu(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
    body: MigrateRequest | None = Body(default=None),
) -> JSONResponse:
    return await _handle_migrate(
        "qemu", vmid, session, endpoint_id, idempotency_key, _actor_label(actor), body
    )


@router.post("/lxc/{vmid}/migrate")
async def migrate_lxc(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
    body: MigrateRequest | None = Body(default=None),
) -> JSONResponse:
    return await _handle_migrate(
        "lxc", vmid, session, endpoint_id, idempotency_key, _actor_label(actor), body
    )


@router.delete("/qemu/{vmid}/migrate/{task_upid}")
async def migrate_cancel_qemu(
    vmid: int,
    task_upid: str,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> JSONResponse:
    return await _handle_migrate_cancel(
        "qemu", vmid, task_upid, session, endpoint_id, _actor_label(actor)
    )


@router.delete("/lxc/{vmid}/migrate/{task_upid}")
async def migrate_cancel_lxc(
    vmid: int,
    task_upid: str,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> JSONResponse:
    return await _handle_migrate_cancel(
        "lxc", vmid, task_upid, session, endpoint_id, _actor_label(actor)
    )


async def _migrate_stream_response(
    vm_type: VmType,
    vmid: int,
    task_upid: str,
    session: SessionDep,
    endpoint_id: int | None,
) -> StreamingResponse | JSONResponse:
    gated = await _gate(session, endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated
    endpoint = gated
    try:
        proxmox = await _open_proxmox_session(endpoint)
    except ProxboxException as error:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "reason": "proxmox_session_unreachable",
                "detail": str(error),
                "endpoint_id": endpoint.id,
            },
        )

    node_or_error = await resolve_proxmox_node(proxmox, vm_type, vmid)
    if isinstance(node_or_error, JSONResponse):
        return node_or_error
    node: str = node_or_error
    assert endpoint.id is not None

    return StreamingResponse(
        _migrate_stream_generator(
            proxmox=proxmox,
            node=node,
            task_upid=task_upid,
            vm_type=vm_type,
            vmid=vmid,
            endpoint_id=endpoint.id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/qemu/{vmid}/migrate/{task_upid}/stream", response_model=None)
async def migrate_stream_qemu(
    vmid: int,
    task_upid: str,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
) -> StreamingResponse | JSONResponse:
    return await _migrate_stream_response("qemu", vmid, task_upid, session, endpoint_id)


@router.get("/lxc/{vmid}/migrate/{task_upid}/stream", response_model=None)
async def migrate_stream_lxc(
    vmid: int,
    task_upid: str,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
) -> StreamingResponse | JSONResponse:
    return await _migrate_stream_response("lxc", vmid, task_upid, session, endpoint_id)

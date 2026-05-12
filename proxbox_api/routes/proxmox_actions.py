"""Operational verb routes (start / stop / snapshot / migrate).

Issue #376. Sub-PR B introduced the gate stub; sub-PR C wired the
``start`` verb; sub-PR D wires ``stop``; sub-PRs E–F wire the rest.
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

from typing import Literal

from fastapi import APIRouter, Header, Query, status
from fastapi.responses import JSONResponse

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.exception import ProxboxException, ProxmoxAPIError
from proxbox_api.logger import logger
from proxbox_api.session.netbox import get_netbox_async_session
from proxbox_api.session.proxmox import ProxmoxSession
from proxbox_api.session.proxmox_providers import _parse_db_endpoint
from proxbox_api.services.idempotency import CacheKey, get_idempotency_cache
from proxbox_api.services.proxmox_helpers import get_vm_status, start_vm, stop_vm
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
) -> JSONResponse:
    """Write the journal entry, cache the response, return JSONResponse.

    Centralises the §6 + §7.3 + §4 cache contracts so the dispatch flow
    above stays readable. ``http_status``/``reason`` are passed only on
    error paths; the success / no-op paths use the §7.3 shape verbatim.
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

    # Cache only the body, not the HTTP status — but only when the
    # caller supplied an Idempotency-Key. The §4 contract reuses the
    # full response, including the 200-OK status, on the second call.
    if cache_key is not None and http_status == status.HTTP_200_OK:
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
) -> JSONResponse:
    return await _handle_stub("snapshot", "qemu", vmid, session, endpoint_id)


@router.post("/lxc/{vmid}/snapshot")
async def snapshot_lxc(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
) -> JSONResponse:
    return await _handle_stub("snapshot", "lxc", vmid, session, endpoint_id)


@router.post("/qemu/{vmid}/migrate")
async def migrate_qemu(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
) -> JSONResponse:
    return await _handle_stub("migrate", "qemu", vmid, session, endpoint_id)


@router.post("/lxc/{vmid}/migrate")
async def migrate_lxc(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
) -> JSONResponse:
    return await _handle_stub("migrate", "lxc", vmid, session, endpoint_id)

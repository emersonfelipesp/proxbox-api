"""Operational verb routes (start / stop / snapshot / migrate) — gate stub.

Issue #376 sub-PR B. The endpoints exist with their final URL shapes so
clients (the netbox-proxbox plugin and external automation) can wire
against a stable surface, but every route returns ``HTTP 403`` with
``reason: "endpoint_writes_disabled"`` (or
``reason: "endpoint_not_found"`` if the caller targeted a non-existent
endpoint) until ``ProxmoxEndpoint.allow_writes`` is set to True for the
target endpoint.

Subsequent sub-PRs (C–F) replace the stub body with the real Proxmox
dispatch path. The 403 gate at the top of every handler is the
load-bearing trust boundary described in ``operational-verbs.md`` §2.3,
layer 3 — it must remain in place after the verbs are wired.

The route handlers accept an optional ``endpoint_id`` query parameter so
callers can target a specific Proxmox cluster among many. When omitted,
the gate cannot resolve a specific endpoint and returns
``reason: "endpoint_id_required"``. The plugin will always pass it once
the backend-proxy view is wired in sub-PR G.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

router = APIRouter()

VmType = Literal["qemu", "lxc"]
Verb = Literal["start", "stop", "snapshot", "migrate"]


async def _gate(
    session: SessionDep, endpoint_id: int | None
) -> JSONResponse | ProxmoxEndpoint:
    """Resolve the target endpoint and enforce ``allow_writes``.

    Returns a 403 ``JSONResponse`` when the gate is closed or the endpoint
    cannot be resolved; otherwise returns the ``ProxmoxEndpoint`` row so
    the caller can keep going. Sub-PRs C–F will replace the "returns
    JSONResponse on failure" pattern with raising a structured exception
    once the dispatch path is in place — for now the inline branch is
    sufficient and keeps the stub readable.
    """
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


def _not_implemented(verb: Verb, vm_type: VmType, vmid: int) -> JSONResponse:
    """Return the stub success-path placeholder used by sub-PR B.

    Reached only when the gate is open. Replaced by the real dispatch in
    sub-PRs C–F. Kept as a single helper so the contract change is
    visible in one place.
    """
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={
            "reason": "verb_not_yet_implemented",
            "detail": (
                f"The {verb!r} verb for {vm_type!r}/{vmid} is gated open but the "
                "dispatch path lands in a follow-up sub-PR (#376 C–F)."
            ),
            "verb": verb,
            "vm_type": vm_type,
            "vmid": vmid,
        },
    )


async def _handle(
    verb: Verb,
    vm_type: VmType,
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None,
) -> JSONResponse:
    gated = await _gate(session, endpoint_id)
    if isinstance(gated, JSONResponse):
        return gated
    return _not_implemented(verb, vm_type, vmid)


@router.post("/qemu/{vmid}/start")
async def start_qemu(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
) -> JSONResponse:
    return await _handle("start", "qemu", vmid, session, endpoint_id)


@router.post("/lxc/{vmid}/start")
async def start_lxc(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
) -> JSONResponse:
    return await _handle("start", "lxc", vmid, session, endpoint_id)


@router.post("/qemu/{vmid}/stop")
async def stop_qemu(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
) -> JSONResponse:
    return await _handle("stop", "qemu", vmid, session, endpoint_id)


@router.post("/lxc/{vmid}/stop")
async def stop_lxc(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
) -> JSONResponse:
    return await _handle("stop", "lxc", vmid, session, endpoint_id)


@router.post("/qemu/{vmid}/snapshot")
async def snapshot_qemu(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
) -> JSONResponse:
    return await _handle("snapshot", "qemu", vmid, session, endpoint_id)


@router.post("/lxc/{vmid}/snapshot")
async def snapshot_lxc(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
) -> JSONResponse:
    return await _handle("snapshot", "lxc", vmid, session, endpoint_id)


@router.post("/qemu/{vmid}/migrate")
async def migrate_qemu(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
) -> JSONResponse:
    return await _handle("migrate", "qemu", vmid, session, endpoint_id)


@router.post("/lxc/{vmid}/migrate")
async def migrate_lxc(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
) -> JSONResponse:
    return await _handle("migrate", "lxc", vmid, session, endpoint_id)

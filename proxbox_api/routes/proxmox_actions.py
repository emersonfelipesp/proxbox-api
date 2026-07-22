"""Operational verb routes (start / stop / snapshot / migrate / lifecycle).

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncGenerator, Literal, TypeVar
from uuid import uuid4

from fastapi import APIRouter, Body, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from proxbox_api.database import AsyncDatabaseSessionDep as SessionDep
from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.exception import ProxboxException, ProxmoxAPIError
from proxbox_api.logger import logger
from proxbox_api.services.idempotency import CacheKey, IdempotencyCache, get_idempotency_cache
from proxbox_api.services.proxmox_helpers import (
    backup_vm,
    cancel_task,
    create_vm_snapshot,
    delete_vm_snapshot,
    get_node_task_status,
    get_vm_status,
    migrate_preflight,
    migrate_vm,
    reboot_vm,
    start_vm,
    stop_vm,
)
from proxbox_api.services.verb_dispatch import (
    build_journal_comments,
    build_success_response,
    resolve_netbox_vm_id,
    resolve_proxmox_node,
    update_verb_journal_entry,
    utcnow_iso,
    write_verb_journal_entry,
)
from proxbox_api.session.netbox import get_netbox_async_session
from proxbox_api.session.proxmox import ProxmoxSession
from proxbox_api.session.proxmox_providers import _parse_db_endpoint
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

router = APIRouter()

T = TypeVar("T")

VmType = Literal["qemu", "lxc"]
JournalKind = Literal["info", "success", "warning", "danger"]
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

LIFECYCLE_WRITES_DISABLED_REASON = "writes_disabled_for_endpoint"

AUDIT_REQUIRED_VERBS: frozenset[Verb] = frozenset(
    (
        "start",
        "stop",
        "snapshot",
        "migrate",
        "reboot",
        "delete",
        "backup",
        "delete_snapshot",
    )
)

_JOURNAL_TERMINAL_SENTINEL = "_proxbox_terminal_finalized"


@dataclass(frozen=True)
class JournalFinalizationResult:
    journal_entry_url: str | None
    finalized: bool
    error: str | None = None
    retry_metadata: dict[str, object] | None = None


async def _gate(
    session: SessionDep,
    endpoint_id: int | None,
    *,
    writes_disabled_reason: str = "endpoint_writes_disabled",
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

    # WHY: write verbs can destroy Proxmox resources, so _gate enforces ProxmoxEndpoint.allow_writes per AGENTS.md section "LLM Agent Safety Guardrails".
    if not endpoint.allow_writes:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "reason": writes_disabled_reason,
                "detail": (
                    "Operational verbs are disabled on this endpoint. Enable "
                    "ProxmoxEndpoint.allow_writes on the NetBox side after "
                    "granting core.run_proxmox_action to the operator group."
                ),
                "endpoint_id": endpoint.id,
            },
        )

    return endpoint


async def _resolve_audit_target_or_error(
    *,
    nb: object,
    endpoint: ProxmoxEndpoint,
    vmid: int,
    verb: Verb,
) -> int | JSONResponse | None:
    """Resolve the NetBox VM journal target before dispatching Proxmox writes."""
    endpoint_id = endpoint.id
    fail_closed = verb in AUDIT_REQUIRED_VERBS
    try:
        netbox_vm_id = await resolve_netbox_vm_id(
            nb,
            vmid,
            endpoint_id=endpoint_id,
            fail_closed=fail_closed,
        )
    except ProxboxException as error:
        detail = error.detail if error.detail is not None else str(error)
        return JSONResponse(
            status_code=error.http_status_code,
            content={
                "reason": "netbox_vm_identity_required_for_audit",
                "detail": detail,
                "verb": verb,
                "vmid": vmid,
                "endpoint_id": endpoint_id,
            },
        )
    if fail_closed and netbox_vm_id is None:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "reason": "netbox_vm_identity_required_for_audit",
                "detail": (
                    "Refusing to dispatch operational verb without a durable "
                    "NetBox VM audit journal target."
                ),
                "verb": verb,
                "vmid": vmid,
                "endpoint_id": endpoint_id,
            },
        )
    return netbox_vm_id


def _journal_entry_id(entry: dict[str, object] | None) -> int | None:
    if entry is None:
        return None
    value = entry.get("id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _journal_entry_url(entry: dict[str, object] | None) -> str | None:
    if entry is None:
        return None
    url = entry.get("url")
    if isinstance(url, str):
        return url
    entry_id = _journal_entry_id(entry)
    if entry_id is not None:
        return f"/api/extras/journal-entries/{entry_id}/"
    return None


def _journal_entry_is_terminal(entry: dict[str, object] | None) -> bool:
    return entry is not None and entry.get(_JOURNAL_TERMINAL_SENTINEL) is True


def _mark_journal_entry_terminal(entry: dict[str, object] | None) -> None:
    if entry is not None:
        entry[_JOURNAL_TERMINAL_SENTINEL] = True


def _error_detail(error: BaseException) -> str:
    message = str(error)
    if message:
        return f"{type(error).__name__}: {message}"
    return type(error).__name__


def _metadata_int(metadata: dict[str, object], key: str) -> int | None:
    value = metadata.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _metadata_str(metadata: dict[str, object], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) else None


def _metadata_kind(metadata: dict[str, object]) -> JournalKind | None:
    value = metadata.get("kind")
    if value in ("info", "success", "warning", "danger"):
        return value
    return None


def _journal_finalization_retry_metadata(
    *,
    journal_entry_id: int,
    journal_entry_url: str | None,
    kind: JournalKind,
    comments: str,
    interrupted_comments: str,
    failed_comments: str,
    terminal_status_code: int,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "journal_entry_id": journal_entry_id,
        "kind": kind,
        "comments": comments,
        "interrupted_comments": interrupted_comments,
        "failed_comments": failed_comments,
        "terminal_status_code": terminal_status_code,
    }
    if journal_entry_url is not None:
        metadata["journal_entry_url"] = journal_entry_url
    return metadata


async def _best_effort_mark_journal_terminal(
    *,
    nb: object,
    journal_entry_id: int,
    writeahead_journal_entry: dict[str, object] | None = None,
    kind: JournalKind,
    comments: str,
    verb: Verb,
    vm_type: VmType,
    vmid: int,
    cause: BaseException,
) -> None:
    try:
        task = asyncio.create_task(
            update_verb_journal_entry(
                nb,
                journal_entry_id=journal_entry_id,
                kind=kind,
                comments=comments,
            )
        )
        await asyncio.shield(task)
        _mark_journal_entry_terminal(writeahead_journal_entry)
    except BaseException as error:  # noqa: BLE001
        logger.error(
            "Best-effort terminal journal mark failed for entry id=%s "
            "%s/%s verb=%s after finalization interruption %s: %s",
            journal_entry_id,
            vm_type,
            vmid,
            verb,
            _error_detail(cause),
            _error_detail(error),
        )


async def _update_journal_entry_resistant_to_cancellation(
    *,
    nb: object,
    journal_entry_id: int,
    writeahead_journal_entry: dict[str, object] | None = None,
    kind: JournalKind,
    comments: str,
    interrupted_comments: str,
    failed_comments: str,
    verb: Verb,
    vm_type: VmType,
    vmid: int,
) -> dict[str, object] | None:
    task = asyncio.create_task(
        update_verb_journal_entry(
            nb,
            journal_entry_id=journal_entry_id,
            kind=kind,
            comments=comments,
        )
    )
    try:
        entry = await asyncio.shield(task)
        _mark_journal_entry_terminal(writeahead_journal_entry)
        _mark_journal_entry_terminal(entry)
        return entry
    except asyncio.CancelledError:
        try:
            if task.done():
                entry = task.result()
            else:
                entry = await task
            _mark_journal_entry_terminal(writeahead_journal_entry)
            _mark_journal_entry_terminal(entry)
        except BaseException as finalization_error:  # noqa: BLE001
            await _best_effort_mark_journal_terminal(
                nb=nb,
                journal_entry_id=journal_entry_id,
                writeahead_journal_entry=writeahead_journal_entry,
                kind="warning",
                comments=interrupted_comments,
                verb=verb,
                vm_type=vm_type,
                vmid=vmid,
                cause=finalization_error,
            )
        raise
    except Exception:
        raise
    except BaseException as error:
        await _best_effort_mark_journal_terminal(
            nb=nb,
            journal_entry_id=journal_entry_id,
            writeahead_journal_entry=writeahead_journal_entry,
            kind="warning",
            comments=failed_comments,
            verb=verb,
            vm_type=vm_type,
            vmid=vmid,
            cause=error,
        )
        raise


def _writeahead_journal_failure_response(
    *,
    verb: Verb,
    vmid: int,
    endpoint_id: int,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "reason": "netbox_vm_identity_required_for_audit",
            "detail": (
                "Refusing to dispatch operational verb because the write-ahead "
                "NetBox VM audit journal entry could not be created."
            ),
            "verb": verb,
            "vmid": vmid,
            "endpoint_id": endpoint_id,
        },
    )


async def _create_writeahead_journal_or_error(
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
    cache: IdempotencyCache,
    cache_key: CacheKey | None,
) -> dict[str, object] | JSONResponse | None:
    """Create the durable pre-dispatch audit journal entry for required verbs."""
    endpoint_id = endpoint.id or 0
    if verb not in AUDIT_REQUIRED_VERBS or netbox_vm_id is None:
        return None

    comments = build_journal_comments(
        verb=verb,
        actor=actor,
        result="in_progress",
        endpoint_name=endpoint.name,
        endpoint_id=endpoint_id,
        dispatched_at=dispatched_at,
        proxmox_task_upid=None,
        idempotency_key=idempotency_key,
        error_detail=None,
    )
    create_task = asyncio.create_task(
        write_verb_journal_entry(
            nb,
            netbox_vm_id=netbox_vm_id,
            kind="info",
            comments=comments,
        )
    )
    try:
        entry = await asyncio.shield(create_task)
    except asyncio.CancelledError as error:
        try:
            entry = create_task.result() if create_task.done() else await create_task
        except BaseException as create_error:  # noqa: BLE001
            logger.warning(
                "Write-ahead journal create did not return an entry after "
                "cancellation for %s/%s verb=%s endpoint=%s: %s",
                vm_type,
                vmid,
                verb,
                endpoint_id,
                _error_detail(create_error),
            )
            raise error from create_error

        if _journal_entry_id(entry) is None:
            logger.warning(
                "Write-ahead journal create returned no id after cancellation "
                "for %s/%s verb=%s endpoint=%s",
                vm_type,
                vmid,
                verb,
                endpoint_id,
            )
            raise

        await _finalize_after_unexpected_dispatch_error(
            error=error,
            phase="writeahead",
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=entry,
            verb=verb,
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
        )
        raise
    except Exception as error:  # noqa: BLE001
        logger.warning(
            "Blocking %s for endpoint=%s vmid=%s because write-ahead journal create failed: %s",
            verb,
            endpoint_id,
            vmid,
            error,
        )
        return _writeahead_journal_failure_response(
            verb=verb,
            vmid=vmid,
            endpoint_id=endpoint_id,
        )

    if _journal_entry_id(entry) is None:
        logger.warning(
            "Blocking %s for endpoint=%s vmid=%s because write-ahead journal create returned no id",
            verb,
            endpoint_id,
            vmid,
        )
        return _writeahead_journal_failure_response(
            verb=verb,
            vmid=vmid,
            endpoint_id=endpoint_id,
        )
    return entry


async def _finalize_journal_entry(
    *,
    nb: object,
    netbox_vm_id: int | None,
    writeahead_journal_entry: dict[str, object] | None,
    verb: Verb,
    vm_type: VmType,
    vmid: int,
    kind: JournalKind,
    comments: str,
    interrupted_comments: str,
    failed_comments: str,
    terminal_status_code: int,
) -> JournalFinalizationResult:
    journal_entry_url = _journal_entry_url(writeahead_journal_entry)
    if _journal_entry_is_terminal(writeahead_journal_entry):
        return JournalFinalizationResult(journal_entry_url=journal_entry_url, finalized=True)

    if netbox_vm_id is None:
        return JournalFinalizationResult(journal_entry_url=journal_entry_url, finalized=True)

    if writeahead_journal_entry is None:
        try:
            entry = await write_verb_journal_entry(
                nb,
                netbox_vm_id=netbox_vm_id,
                kind=kind,
                comments=comments,
            )
        except Exception as error:  # noqa: BLE001
            return JournalFinalizationResult(
                journal_entry_url=journal_entry_url,
                finalized=False,
                error=_error_detail(error),
            )
        return JournalFinalizationResult(
            journal_entry_url=_journal_entry_url(entry),
            finalized=True,
        )

    entry_id = _journal_entry_id(writeahead_journal_entry)
    if entry_id is None:
        error_detail = "write-ahead journal entry returned without an id"
        logger.warning(
            "Write-ahead journal entry for %s/%s verb=%s had no id; "
            "cannot apply terminal journal update",
            vm_type,
            vmid,
            verb,
        )
        return JournalFinalizationResult(
            journal_entry_url=journal_entry_url,
            finalized=False,
            error=error_detail,
        )

    try:
        entry = await _update_journal_entry_resistant_to_cancellation(
            nb=nb,
            journal_entry_id=entry_id,
            writeahead_journal_entry=writeahead_journal_entry,
            kind=kind,
            comments=comments,
            interrupted_comments=interrupted_comments,
            failed_comments=failed_comments,
            verb=verb,
            vm_type=vm_type,
            vmid=vmid,
        )
    except Exception as error:  # noqa: BLE001
        error_detail = _error_detail(error)
        logger.error(
            "Failed to finalize write-ahead journal entry id=%s for "
            "%s/%s verb=%s after Proxmox result resolved: %s",
            entry_id,
            vm_type,
            vmid,
            verb,
            error_detail,
        )
        return JournalFinalizationResult(
            journal_entry_url=journal_entry_url,
            finalized=False,
            error=error_detail,
            retry_metadata=_journal_finalization_retry_metadata(
                journal_entry_id=entry_id,
                journal_entry_url=journal_entry_url,
                kind=kind,
                comments=comments,
                interrupted_comments=interrupted_comments,
                failed_comments=failed_comments,
                terminal_status_code=terminal_status_code,
            ),
        )

    return JournalFinalizationResult(
        journal_entry_url=_journal_entry_url(entry) or journal_entry_url,
        finalized=True,
    )


async def _retry_cached_journal_finalization(
    *,
    nb: object,
    metadata: dict[str, object],
    verb: Verb,
    vm_type: VmType,
    vmid: int,
) -> JournalFinalizationResult:
    entry_id = _metadata_int(metadata, "journal_entry_id")
    kind = _metadata_kind(metadata)
    comments = _metadata_str(metadata, "comments")
    interrupted_comments = _metadata_str(metadata, "interrupted_comments") or comments
    failed_comments = _metadata_str(metadata, "failed_comments") or comments
    journal_entry_url = _metadata_str(metadata, "journal_entry_url")
    if (
        entry_id is None
        or kind is None
        or comments is None
        or interrupted_comments is None
        or failed_comments is None
    ):
        return JournalFinalizationResult(
            journal_entry_url=journal_entry_url,
            finalized=False,
            error="cached journal finalization metadata is incomplete",
            retry_metadata=metadata,
        )

    try:
        entry = await _update_journal_entry_resistant_to_cancellation(
            nb=nb,
            journal_entry_id=entry_id,
            kind=kind,
            comments=comments,
            interrupted_comments=interrupted_comments,
            failed_comments=failed_comments,
            verb=verb,
            vm_type=vm_type,
            vmid=vmid,
        )
    except Exception as error:  # noqa: BLE001
        error_detail = _error_detail(error)
        logger.error(
            "Retrying cached journal finalization failed for entry id=%s %s/%s verb=%s: %s",
            entry_id,
            vm_type,
            vmid,
            verb,
            error_detail,
        )
        return JournalFinalizationResult(
            journal_entry_url=journal_entry_url,
            finalized=False,
            error=error_detail,
            retry_metadata=metadata,
        )

    return JournalFinalizationResult(
        journal_entry_url=_journal_entry_url(entry) or journal_entry_url,
        finalized=True,
    )


async def _cached_idempotency_response(
    *,
    cache: IdempotencyCache,
    cache_key: CacheKey,
    nb: object,
    verb: Verb,
    vm_type: VmType,
    vmid: int,
) -> JSONResponse | None:
    cached = await cache.get_entry(cache_key)
    if cached is None:
        return None

    body = dict(cached.response)
    status_code = cached.status_code
    metadata = cached.journal_finalization
    if body.get("journal_finalized") is False and metadata is not None:
        finalization = await _retry_cached_journal_finalization(
            nb=nb,
            metadata=metadata,
            verb=verb,
            vm_type=vm_type,
            vmid=vmid,
        )
        if finalization.finalized:
            body.pop("journal_finalized", None)
            body.pop("finalization_error", None)
            if body.get("reason") == "netbox_journal_finalization_failed":
                body.pop("reason", None)
            if finalization.journal_entry_url is not None:
                body["journal_entry_url"] = finalization.journal_entry_url
            terminal_status = _metadata_int(metadata, "terminal_status_code")
            if terminal_status is not None:
                status_code = terminal_status
            await cache.store(cache_key, body, status_code=status_code)
        else:
            body["journal_finalized"] = False
            if finalization.error is not None:
                body["finalization_error"] = finalization.error
            body.setdefault("reason", "netbox_journal_finalization_failed")
            await cache.store(
                cache_key,
                body,
                status_code=status_code,
                journal_finalization=finalization.retry_metadata or metadata,
            )

    return JSONResponse(status_code=status_code, content=body)


def _audit_response_body(
    *,
    verb: Verb,
    vm_type: VmType,
    vmid: int,
    endpoint_id: int,
    result: str,
    dispatched_at: str,
    proxmox_task_upid: str | None,
    journal_entry_url: str | None,
    http_status: int,
    reason: str | None,
    error_detail: str | None,
    extra: dict[str, object] | None,
) -> dict[str, object]:
    body = build_success_response(
        verb=verb,
        vm_type=vm_type,
        vmid=vmid,
        endpoint_id=endpoint_id,
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
    return body


async def _store_pending_journal_finalization_cache(
    *,
    cache: IdempotencyCache,
    cache_key: CacheKey | None,
    writeahead_journal_entry: dict[str, object] | None,
    verb: Verb,
    vm_type: VmType,
    vmid: int,
    endpoint_id: int,
    result: str,
    dispatched_at: str,
    proxmox_task_upid: str | None,
    http_status: int,
    reason: str | None,
    error_detail: str | None,
    extra: dict[str, object] | None,
    kind: JournalKind,
    comments: str,
    interrupted_comments: str,
    failed_comments: str,
) -> dict[str, object] | None:
    if cache_key is None:
        return None

    writeahead_entry_id = _journal_entry_id(writeahead_journal_entry)
    if writeahead_entry_id is None:
        return None

    journal_entry_url = _journal_entry_url(writeahead_journal_entry)
    retry_metadata = _journal_finalization_retry_metadata(
        journal_entry_id=writeahead_entry_id,
        journal_entry_url=journal_entry_url,
        kind=kind,
        comments=comments,
        interrupted_comments=interrupted_comments,
        failed_comments=failed_comments,
        terminal_status_code=http_status,
    )
    body = _audit_response_body(
        verb=verb,
        vm_type=vm_type,
        vmid=vmid,
        endpoint_id=endpoint_id,
        result=result,
        dispatched_at=dispatched_at,
        proxmox_task_upid=proxmox_task_upid,
        journal_entry_url=journal_entry_url,
        http_status=http_status,
        reason=reason,
        error_detail=error_detail,
        extra=extra,
    )
    body["journal_finalized"] = False
    body["finalization_error"] = "terminal journal finalization is pending"
    body.setdefault("reason", "netbox_journal_finalization_failed")
    await cache.store(
        cache_key,
        body,
        status_code=status.HTTP_502_BAD_GATEWAY,
        journal_finalization=retry_metadata,
    )
    return retry_metadata


def _unexpected_dispatch_result(error: BaseException) -> tuple[str, str]:
    if isinstance(error, asyncio.CancelledError):
        return (
            "interrupted",
            "Operation interrupted before the Proxmox call returned.",
        )
    message = str(error) or type(error).__name__
    return "failed", f"{type(error).__name__}: {message}"


async def _finalize_after_unexpected_dispatch_error(
    *,
    error: BaseException,
    phase: str,
    nb: object,
    netbox_vm_id: int | None,
    writeahead_journal_entry: dict[str, object] | None,
    verb: Verb,
    vm_type: VmType,
    vmid: int,
    endpoint: ProxmoxEndpoint,
    actor: str,
    dispatched_at: str,
    idempotency_key: str | None,
    cache: IdempotencyCache,
    cache_key: CacheKey | None,
    proxmox_task_upid: str | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    if _journal_entry_is_terminal(writeahead_journal_entry):
        logger.info(
            "Skipping %s journal finalization for %s/%s verb=%s because entry is already terminal",
            phase,
            vm_type,
            vmid,
            verb,
        )
        return

    result, error_detail = _unexpected_dispatch_result(error)
    logger.warning(
        "Finalizing write-ahead journal for %s/%s verb=%s after %s %s: %s",
        vm_type,
        vmid,
        verb,
        phase,
        result,
        error_detail,
    )
    try:
        await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb=verb,
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            result=result,
            kind="warning",
            proxmox_task_upid=proxmox_task_upid,
            error_detail=error_detail,
            http_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            reason="proxmox_dispatch_interrupted"
            if isinstance(error, asyncio.CancelledError)
            else "proxmox_dispatch_unexpected_error",
            extra=extra,
        )
    except BaseException as finalize_error:  # noqa: BLE001
        logger.warning(
            "Failed to finalize write-ahead journal for %s/%s verb=%s after %s error: %s",
            vm_type,
            vmid,
            verb,
            phase,
            finalize_error,
        )


async def _await_with_interruption_journal(
    awaitable: Awaitable[T],
    *,
    phase: str,
    nb: object,
    netbox_vm_id: int | None,
    writeahead_journal_entry: dict[str, object] | None,
    verb: Verb,
    vm_type: VmType,
    vmid: int,
    endpoint: ProxmoxEndpoint,
    actor: str,
    dispatched_at: str,
    idempotency_key: str | None,
    cache: object,
    cache_key: CacheKey | None,
    proxmox_task_upid: str | None = None,
    extra: dict[str, object] | None = None,
) -> T:
    try:
        return await awaitable
    except ProxmoxAPIError:
        raise
    except BaseException as error:
        await _finalize_after_unexpected_dispatch_error(
            error=error,
            phase=phase,
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb=verb,
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            proxmox_task_upid=proxmox_task_upid,
            extra=extra,
        )
        raise


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


async def delete_vm_via_intent_dispatcher(
    endpoint: ProxmoxEndpoint,
    session: SessionDep,
    vm_type: VmType,
    vmid: int,
    node: str,
    *,
    actor: str,
    suppress_dispatcher_journal: bool = False,
) -> str | None:
    """Destroy a VM through the existing intent deletion dispatcher."""
    from proxbox_api.routes.intent.dispatchers.common import IntentEndpointContext
    from proxbox_api.routes.intent.dispatchers.lxc_destroy import dispatch_lxc_destroy
    from proxbox_api.routes.intent.dispatchers.qemu_destroy import dispatch_qemu_destroy

    endpoint_id = endpoint.id
    assert endpoint_id is not None
    endpoint_context = IntentEndpointContext(session=session, endpoint_id=endpoint_id)
    run_uuid = uuid4()

    try:
        if vm_type == "qemu":
            result = await dispatch_qemu_destroy(
                endpoint_context,
                vmid,
                node,
                run_uuid,
                actor=actor,
                suppress_journal=suppress_dispatcher_journal,
            )
        else:
            result = await dispatch_lxc_destroy(
                endpoint_context,
                vmid,
                node,
                run_uuid,
                actor=actor,
                suppress_journal=suppress_dispatcher_journal,
            )
    except HTTPException as error:
        raise ProxmoxAPIError(message=str(error.detail), original_error=error) from error

    upid = result.get("upid")
    return str(upid) if upid is not None else None


def _delete_extra(stop_task_upid: str | None) -> dict[str, object] | None:
    if stop_task_upid is None:
        return None
    return {"stop_task_upid": stop_task_upid}


async def _prepare_delete_dispatch(
    *,
    proxmox: object,
    node: str,
    vm_type: VmType,
    vmid: int,
    nb: object,
    netbox_vm_id: int | None,
    writeahead_journal_entry: dict[str, object] | None,
    endpoint: ProxmoxEndpoint,
    actor: str,
    dispatched_at: str,
    idempotency_key: str | None,
    cache: object,
    cache_key: CacheKey | None,
) -> tuple[str | None, JSONResponse | None]:
    try:
        current = await get_vm_status(proxmox, node, vm_type, vmid)
    except ProxmoxAPIError as error:
        response = await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="delete",
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
        return None, response

    if not _is_running(vm_type, current):
        return None, None

    try:
        stop_task_upid = await stop_vm(proxmox, node, vm_type, vmid)
    except ProxmoxAPIError as error:
        response = await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="delete",
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
        return None, response

    return stop_task_upid, None


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
        cache_key = CacheKey(endpoint_id=endpoint_id, verb="start", vmid=vmid, key=idempotency_key)
        cached_response = await _cached_idempotency_response(
            cache=cache,
            cache_key=cache_key,
            nb=nb,
            verb="start",
            vm_type=vm_type,
            vmid=vmid,
        )
        if cached_response is not None:
            return cached_response

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

    netbox_vm_id_or_error = await _resolve_audit_target_or_error(
        nb=nb, endpoint=endpoint, vmid=vmid, verb="start"
    )
    if isinstance(netbox_vm_id_or_error, JSONResponse):
        return netbox_vm_id_or_error
    netbox_vm_id = netbox_vm_id_or_error
    dispatched_at = utcnow_iso()
    writeahead_journal_entry = await _create_writeahead_journal_or_error(
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
    )
    if isinstance(writeahead_journal_entry, JSONResponse):
        return writeahead_journal_entry

    # State-based no-op pre-flight (§4.2). Reached before any cache write.
    try:
        current = await _await_with_interruption_journal(
            get_vm_status(proxmox, node, vm_type, vmid),
            phase="status",
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="start",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
        )
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
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
            writeahead_journal_entry=writeahead_journal_entry,
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
        upid = await _await_with_interruption_journal(
            start_vm(proxmox, node, vm_type, vmid),
            phase="dispatch",
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="start",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
        )
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
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
        writeahead_journal_entry=writeahead_journal_entry,
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
        cache_key = CacheKey(endpoint_id=endpoint_id, verb="stop", vmid=vmid, key=idempotency_key)
        cached_response = await _cached_idempotency_response(
            cache=cache,
            cache_key=cache_key,
            nb=nb,
            verb="stop",
            vm_type=vm_type,
            vmid=vmid,
        )
        if cached_response is not None:
            return cached_response

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

    netbox_vm_id_or_error = await _resolve_audit_target_or_error(
        nb=nb, endpoint=endpoint, vmid=vmid, verb="stop"
    )
    if isinstance(netbox_vm_id_or_error, JSONResponse):
        return netbox_vm_id_or_error
    netbox_vm_id = netbox_vm_id_or_error
    dispatched_at = utcnow_iso()
    writeahead_journal_entry = await _create_writeahead_journal_or_error(
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
    )
    if isinstance(writeahead_journal_entry, JSONResponse):
        return writeahead_journal_entry

    try:
        current = await _await_with_interruption_journal(
            get_vm_status(proxmox, node, vm_type, vmid),
            phase="status",
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="stop",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
        )
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
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
            writeahead_journal_entry=writeahead_journal_entry,
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
        upid = await _await_with_interruption_journal(
            stop_vm(proxmox, node, vm_type, vmid),
            phase="dispatch",
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="stop",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
        )
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
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
        writeahead_journal_entry=writeahead_journal_entry,
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


async def _dispatch_reboot(
    *,
    endpoint: ProxmoxEndpoint,
    vm_type: VmType,
    vmid: int,
    nb: object,
    idempotency_key: str | None,
    actor: str,
) -> JSONResponse:
    """Execute the reboot verb: resolve node, pre-flight, dispatch, audit.

    Reboot mirrors ``start``/``stop``: it checks current state first and
    treats an already-stopped guest as a no-op instead of asking Proxmox
    to reboot something that is not running.
    """
    endpoint_id = endpoint.id
    assert endpoint_id is not None

    cache = get_idempotency_cache()
    cache_key: CacheKey | None = None
    if idempotency_key:
        cache_key = CacheKey(endpoint_id=endpoint_id, verb="reboot", vmid=vmid, key=idempotency_key)
        cached_response = await _cached_idempotency_response(
            cache=cache,
            cache_key=cache_key,
            nb=nb,
            verb="reboot",
            vm_type=vm_type,
            vmid=vmid,
        )
        if cached_response is not None:
            return cached_response

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

    netbox_vm_id_or_error = await _resolve_audit_target_or_error(
        nb=nb, endpoint=endpoint, vmid=vmid, verb="reboot"
    )
    if isinstance(netbox_vm_id_or_error, JSONResponse):
        return netbox_vm_id_or_error
    netbox_vm_id = netbox_vm_id_or_error
    dispatched_at = utcnow_iso()
    writeahead_journal_entry = await _create_writeahead_journal_or_error(
        nb=nb,
        netbox_vm_id=netbox_vm_id,
        verb="reboot",
        vm_type=vm_type,
        vmid=vmid,
        endpoint=endpoint,
        actor=actor,
        dispatched_at=dispatched_at,
        idempotency_key=idempotency_key,
        cache=cache,
        cache_key=cache_key,
    )
    if isinstance(writeahead_journal_entry, JSONResponse):
        return writeahead_journal_entry

    try:
        current = await _await_with_interruption_journal(
            get_vm_status(proxmox, node, vm_type, vmid),
            phase="status",
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="reboot",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
        )
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="reboot",
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
            writeahead_journal_entry=writeahead_journal_entry,
            verb="reboot",
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
        upid = await _await_with_interruption_journal(
            reboot_vm(proxmox, node, vm_type, vmid),
            phase="dispatch",
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="reboot",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
        )
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="reboot",
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
        writeahead_journal_entry=writeahead_journal_entry,
        verb="reboot",
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


async def _dispatch_delete(
    *,
    endpoint: ProxmoxEndpoint,
    session: SessionDep,
    vm_type: VmType,
    vmid: int,
    nb: object,
    idempotency_key: str | None,
    actor: str,
) -> JSONResponse:
    """Execute the delete verb: stop if running, delete, audit once."""
    endpoint_id = endpoint.id
    assert endpoint_id is not None

    cache = get_idempotency_cache()
    cache_key: CacheKey | None = None
    if idempotency_key:
        cache_key = CacheKey(endpoint_id=endpoint_id, verb="delete", vmid=vmid, key=idempotency_key)
        cached_response = await _cached_idempotency_response(
            cache=cache,
            cache_key=cache_key,
            nb=nb,
            verb="delete",
            vm_type=vm_type,
            vmid=vmid,
        )
        if cached_response is not None:
            return cached_response

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

    netbox_vm_id_or_error = await _resolve_audit_target_or_error(
        nb=nb, endpoint=endpoint, vmid=vmid, verb="delete"
    )
    if isinstance(netbox_vm_id_or_error, JSONResponse):
        return netbox_vm_id_or_error
    netbox_vm_id = netbox_vm_id_or_error
    dispatched_at = utcnow_iso()
    writeahead_journal_entry = await _create_writeahead_journal_or_error(
        nb=nb,
        netbox_vm_id=netbox_vm_id,
        verb="delete",
        vm_type=vm_type,
        vmid=vmid,
        endpoint=endpoint,
        actor=actor,
        dispatched_at=dispatched_at,
        idempotency_key=idempotency_key,
        cache=cache,
        cache_key=cache_key,
    )
    if isinstance(writeahead_journal_entry, JSONResponse):
        return writeahead_journal_entry

    stop_task_upid, prepare_response = await _await_with_interruption_journal(
        _prepare_delete_dispatch(
            proxmox=proxmox,
            node=node,
            vm_type=vm_type,
            vmid=vmid,
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
        ),
        phase="delete_prepare",
        nb=nb,
        netbox_vm_id=netbox_vm_id,
        writeahead_journal_entry=writeahead_journal_entry,
        verb="delete",
        vm_type=vm_type,
        vmid=vmid,
        endpoint=endpoint,
        actor=actor,
        dispatched_at=dispatched_at,
        idempotency_key=idempotency_key,
        cache=cache,
        cache_key=cache_key,
    )
    if prepare_response is not None:
        return prepare_response

    extra = _delete_extra(stop_task_upid)
    try:
        upid = await _await_with_interruption_journal(
            delete_vm_via_intent_dispatcher(
                endpoint,
                session,
                vm_type,
                vmid,
                node,
                actor=actor,
                suppress_dispatcher_journal=True,
            ),
            phase="dispatch",
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="delete",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            extra=extra,
        )
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="delete",
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
            extra=extra,
        )

    return await _audit_and_respond(
        nb=nb,
        netbox_vm_id=netbox_vm_id,
        writeahead_journal_entry=writeahead_journal_entry,
        verb="delete",
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
        extra=extra,
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
        cached_response = await _cached_idempotency_response(
            cache=cache,
            cache_key=cache_key,
            nb=nb,
            verb="snapshot",
            vm_type=vm_type,
            vmid=vmid,
        )
        if cached_response is not None:
            return cached_response

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

    netbox_vm_id_or_error = await _resolve_audit_target_or_error(
        nb=nb, endpoint=endpoint, vmid=vmid, verb="snapshot"
    )
    if isinstance(netbox_vm_id_or_error, JSONResponse):
        return netbox_vm_id_or_error
    netbox_vm_id = netbox_vm_id_or_error
    dispatched_at = utcnow_iso()
    writeahead_journal_entry = await _create_writeahead_journal_or_error(
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
    )
    if isinstance(writeahead_journal_entry, JSONResponse):
        return writeahead_journal_entry

    effective_snapname = snapname or _default_snapname(idempotency_key)

    try:
        upid = await _await_with_interruption_journal(
            create_vm_snapshot(proxmox, node, vm_type, vmid, effective_snapname, description),
            phase="dispatch",
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="snapshot",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            extra={"snapname": effective_snapname},
        )
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
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
        writeahead_journal_entry=writeahead_journal_entry,
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


class BackupRequest(BaseModel):
    """Request body for the backup verb.

    ``storage`` is required but validated after the §2.3 gate so a
    write-disabled endpoint still returns 403 instead of leaking body
    validation details.
    """

    storage: str | None = None
    mode: str = "snapshot"
    compress: str = "zstd"
    notes: str | None = None


async def _dispatch_backup(
    *,
    endpoint: ProxmoxEndpoint,
    vm_type: VmType,
    vmid: int,
    nb: object,
    idempotency_key: str | None,
    actor: str,
    storage_name: str,
    mode: str,
    compress: str,
    notes: str | None,
) -> JSONResponse:
    """Execute the backup verb: resolve node, dispatch vzdump, audit."""
    endpoint_id = endpoint.id
    assert endpoint_id is not None

    cache = get_idempotency_cache()
    cache_key: CacheKey | None = None
    if idempotency_key:
        cache_key = CacheKey(endpoint_id=endpoint_id, verb="backup", vmid=vmid, key=idempotency_key)
        cached_response = await _cached_idempotency_response(
            cache=cache,
            cache_key=cache_key,
            nb=nb,
            verb="backup",
            vm_type=vm_type,
            vmid=vmid,
        )
        if cached_response is not None:
            return cached_response

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

    netbox_vm_id_or_error = await _resolve_audit_target_or_error(
        nb=nb, endpoint=endpoint, vmid=vmid, verb="backup"
    )
    if isinstance(netbox_vm_id_or_error, JSONResponse):
        return netbox_vm_id_or_error
    netbox_vm_id = netbox_vm_id_or_error
    dispatched_at = utcnow_iso()
    writeahead_journal_entry = await _create_writeahead_journal_or_error(
        nb=nb,
        netbox_vm_id=netbox_vm_id,
        verb="backup",
        vm_type=vm_type,
        vmid=vmid,
        endpoint=endpoint,
        actor=actor,
        dispatched_at=dispatched_at,
        idempotency_key=idempotency_key,
        cache=cache,
        cache_key=cache_key,
    )
    if isinstance(writeahead_journal_entry, JSONResponse):
        return writeahead_journal_entry

    try:
        extra: dict[str, object] = {
            "storage": storage_name,
            "mode": mode,
            "compress": compress,
        }
        if notes is not None:
            extra["notes"] = notes
        upid = await _await_with_interruption_journal(
            backup_vm(
                proxmox,
                node,
                vmid,
                storage=storage_name,
                mode=mode,
                compress=compress,
                notes=notes,
            ),
            phase="dispatch",
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="backup",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            extra=extra,
        )
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="backup",
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
            extra=extra,
        )

    return await _audit_and_respond(
        nb=nb,
        netbox_vm_id=netbox_vm_id,
        writeahead_journal_entry=writeahead_journal_entry,
        verb="backup",
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
        extra=extra,
    )


async def _dispatch_delete_snapshot(
    *,
    endpoint: ProxmoxEndpoint,
    vm_type: VmType,
    vmid: int,
    nb: object,
    idempotency_key: str | None,
    actor: str,
    snapname: str,
) -> JSONResponse:
    """Execute the delete-snapshot verb: resolve node, dispatch, audit."""
    endpoint_id = endpoint.id
    assert endpoint_id is not None

    cache = get_idempotency_cache()
    cache_key: CacheKey | None = None
    if idempotency_key:
        cache_key = CacheKey(
            endpoint_id=endpoint_id, verb="delete_snapshot", vmid=vmid, key=idempotency_key
        )
        cached_response = await _cached_idempotency_response(
            cache=cache,
            cache_key=cache_key,
            nb=nb,
            verb="delete_snapshot",
            vm_type=vm_type,
            vmid=vmid,
        )
        if cached_response is not None:
            return cached_response

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

    netbox_vm_id_or_error = await _resolve_audit_target_or_error(
        nb=nb, endpoint=endpoint, vmid=vmid, verb="delete_snapshot"
    )
    if isinstance(netbox_vm_id_or_error, JSONResponse):
        return netbox_vm_id_or_error
    netbox_vm_id = netbox_vm_id_or_error
    dispatched_at = utcnow_iso()
    writeahead_journal_entry = await _create_writeahead_journal_or_error(
        nb=nb,
        netbox_vm_id=netbox_vm_id,
        verb="delete_snapshot",
        vm_type=vm_type,
        vmid=vmid,
        endpoint=endpoint,
        actor=actor,
        dispatched_at=dispatched_at,
        idempotency_key=idempotency_key,
        cache=cache,
        cache_key=cache_key,
    )
    if isinstance(writeahead_journal_entry, JSONResponse):
        return writeahead_journal_entry

    try:
        upid = await _await_with_interruption_journal(
            delete_vm_snapshot(proxmox, node, vm_type, vmid, snapname),
            phase="dispatch",
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="delete_snapshot",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            extra={"snapname": snapname},
        )
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="delete_snapshot",
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
            extra={"snapname": snapname},
        )

    return await _audit_and_respond(
        nb=nb,
        netbox_vm_id=netbox_vm_id,
        writeahead_journal_entry=writeahead_journal_entry,
        verb="delete_snapshot",
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
        extra={"snapname": snapname},
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
        cached_response = await _cached_idempotency_response(
            cache=cache,
            cache_key=cache_key,
            nb=nb,
            verb="migrate",
            vm_type=vm_type,
            vmid=vmid,
        )
        if cached_response is not None:
            return cached_response

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

    netbox_vm_id_or_error = await _resolve_audit_target_or_error(
        nb=nb, endpoint=endpoint, vmid=vmid, verb="migrate"
    )
    if isinstance(netbox_vm_id_or_error, JSONResponse):
        return netbox_vm_id_or_error
    netbox_vm_id = netbox_vm_id_or_error
    dispatched_at = utcnow_iso()
    writeahead_journal_entry = await _create_writeahead_journal_or_error(
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
    )
    if isinstance(writeahead_journal_entry, JSONResponse):
        return writeahead_journal_entry

    # §9 preflight: GET nodes/{node}/{vm_type}/{vmid}/migrate, then
    # apply the three reject conditions before any state mutation.
    try:
        preflight = await _await_with_interruption_journal(
            migrate_preflight(proxmox, node, vm_type, vmid),
            phase="preflight",
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="migrate",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
        )
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
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
            writeahead_journal_entry=writeahead_journal_entry,
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
        upid = await _await_with_interruption_journal(
            migrate_vm(proxmox, node, vm_type, vmid, target, online),
            phase="dispatch",
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="migrate",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=idempotency_key,
            cache=cache,
            cache_key=cache_key,
            extra={"target": target, "online": online, "source_node": node},
        )
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
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
        writeahead_journal_entry=writeahead_journal_entry,
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
    writeahead_journal_entry: dict[str, object] | None = None,
    verb: Verb,
    vm_type: VmType,
    vmid: int,
    endpoint: ProxmoxEndpoint,
    actor: str,
    dispatched_at: str,
    idempotency_key: str | None,
    cache: IdempotencyCache,
    cache_key: CacheKey | None,
    result: str,
    kind: JournalKind,
    proxmox_task_upid: str | None,
    error_detail: str | None,
    http_status: int = status.HTTP_200_OK,
    reason: str | None = None,
    extra: dict[str, object] | None = None,
) -> JSONResponse:
    """Finalize the journal entry, cache the response, return JSONResponse.

    Centralises the §6 + §7.3 + §4 cache contracts so the dispatch flow
    above stays readable. ``http_status``/``reason`` are passed only on
    error paths; the success / no-op paths use the §7.3 shape verbatim.
    ``extra`` carries verb-specific fields (e.g. snapshot's ``snapname``)
    that the §7.3 base shape doesn't model. When ``writeahead_journal_entry``
    is supplied, the existing NetBox record is patched in place instead of
    creating a second journal entry.
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
    interrupted_comments = build_journal_comments(
        verb=verb,
        actor=actor,
        result="interrupted",
        endpoint_name=endpoint.name,
        endpoint_id=endpoint.id or 0,
        dispatched_at=dispatched_at,
        proxmox_task_upid=proxmox_task_upid,
        idempotency_key=idempotency_key,
        error_detail="Journal finalization was interrupted before the terminal state was committed.",
    )
    failed_comments = build_journal_comments(
        verb=verb,
        actor=actor,
        result="failed",
        endpoint_name=endpoint.name,
        endpoint_id=endpoint.id or 0,
        dispatched_at=dispatched_at,
        proxmox_task_upid=proxmox_task_upid,
        idempotency_key=idempotency_key,
        error_detail="Journal finalization failed before the terminal state was committed.",
    )
    endpoint_id = endpoint.id or 0
    preliminary_retry_metadata = None
    if not _journal_entry_is_terminal(writeahead_journal_entry):
        preliminary_retry_metadata = await _store_pending_journal_finalization_cache(
            cache=cache,
            cache_key=cache_key,
            writeahead_journal_entry=writeahead_journal_entry,
            verb=verb,
            vm_type=vm_type,
            vmid=vmid,
            endpoint_id=endpoint_id,
            result=result,
            dispatched_at=dispatched_at,
            proxmox_task_upid=proxmox_task_upid,
            http_status=http_status,
            reason=reason,
            error_detail=error_detail,
            extra=extra,
            kind=kind,
            comments=comments,
            interrupted_comments=interrupted_comments,
            failed_comments=failed_comments,
        )

    finalization = await _finalize_journal_entry(
        nb=nb,
        netbox_vm_id=netbox_vm_id,
        writeahead_journal_entry=writeahead_journal_entry,
        verb=verb,
        vm_type=vm_type,
        vmid=vmid,
        kind=kind,
        comments=comments,
        interrupted_comments=interrupted_comments,
        failed_comments=failed_comments,
        terminal_status_code=http_status,
    )

    body = _audit_response_body(
        verb=verb,
        vm_type=vm_type,
        vmid=vmid,
        endpoint_id=endpoint_id,
        result=result,
        dispatched_at=dispatched_at,
        proxmox_task_upid=proxmox_task_upid,
        journal_entry_url=finalization.journal_entry_url,
        http_status=http_status,
        reason=reason,
        error_detail=error_detail,
        extra=extra,
    )

    response_status = http_status
    if not finalization.finalized:
        response_status = http_status if http_status >= 400 else status.HTTP_502_BAD_GATEWAY
        body["journal_finalized"] = False
        body["finalization_error"] = finalization.error or "terminal journal finalization failed"
        body.setdefault("reason", "netbox_journal_finalization_failed")

    # Cache only when the caller supplied an Idempotency-Key. If the
    # Proxmox mutation has resolved but the terminal journal PATCH has
    # not, keep private metadata so a retry patches the existing entry
    # instead of re-dispatching the Proxmox verb, which is unsafe because
    # these mutations are not idempotent on the Proxmox side.
    if cache_key is not None and finalization.finalized:
        await cache.store(cache_key, body, status_code=response_status)
    elif cache_key is not None and (
        finalization.retry_metadata is not None or preliminary_retry_metadata is not None
    ):
        await cache.store(
            cache_key,
            body,
            status_code=response_status,
            journal_finalization=finalization.retry_metadata or preliminary_retry_metadata,
        )

    return JSONResponse(status_code=response_status, content=body)


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


def _idempotency_cache_key(
    *,
    endpoint: ProxmoxEndpoint,
    verb: Verb,
    vmid: int,
    idempotency_key: str | None,
) -> CacheKey | None:
    endpoint_id = endpoint.id
    if idempotency_key is None or endpoint_id is None:
        return None
    return CacheKey(endpoint_id=endpoint_id, verb=verb, vmid=vmid, key=idempotency_key)


async def _run_with_idempotency_single_flight(
    cache_key: CacheKey | None,
    operation: Callable[[], Awaitable[JSONResponse]],
) -> JSONResponse:
    if cache_key is None:
        return await operation()
    cache = get_idempotency_cache()
    async with cache.single_flight(cache_key):
        return await operation()


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
    return await _run_with_idempotency_single_flight(
        _idempotency_cache_key(
            endpoint=gated,
            verb="start",
            vmid=vmid,
            idempotency_key=idempotency_key,
        ),
        lambda: _dispatch_start(
            endpoint=gated,
            vm_type=vm_type,
            vmid=vmid,
            nb=nb_session,
            idempotency_key=idempotency_key,
            actor=actor,
        ),
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
    return await _run_with_idempotency_single_flight(
        _idempotency_cache_key(
            endpoint=gated,
            verb="stop",
            vmid=vmid,
            idempotency_key=idempotency_key,
        ),
        lambda: _dispatch_stop(
            endpoint=gated,
            vm_type=vm_type,
            vmid=vmid,
            nb=nb_session,
            idempotency_key=idempotency_key,
            actor=actor,
        ),
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
    return await _run_with_idempotency_single_flight(
        _idempotency_cache_key(
            endpoint=gated,
            verb="snapshot",
            vmid=vmid,
            idempotency_key=idempotency_key,
        ),
        lambda: _dispatch_snapshot(
            endpoint=gated,
            vm_type=vm_type,
            vmid=vmid,
            nb=nb_session,
            idempotency_key=idempotency_key,
            actor=actor,
            snapname=snapname,
            description=description,
        ),
    )


async def _handle_reboot(
    vm_type: VmType,
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None,
    idempotency_key: str | None,
    actor: str,
) -> JSONResponse:
    gated = await _gate(
        session,
        endpoint_id,
        writes_disabled_reason=LIFECYCLE_WRITES_DISABLED_REASON,
    )
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
    return await _run_with_idempotency_single_flight(
        _idempotency_cache_key(
            endpoint=gated,
            verb="reboot",
            vmid=vmid,
            idempotency_key=idempotency_key,
        ),
        lambda: _dispatch_reboot(
            endpoint=gated,
            vm_type=vm_type,
            vmid=vmid,
            nb=nb_session,
            idempotency_key=idempotency_key,
            actor=actor,
        ),
    )


async def _handle_delete(
    vm_type: VmType,
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None,
    idempotency_key: str | None,
    actor: str,
) -> JSONResponse:
    gated = await _gate(
        session,
        endpoint_id,
        writes_disabled_reason=LIFECYCLE_WRITES_DISABLED_REASON,
    )
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
    return await _run_with_idempotency_single_flight(
        _idempotency_cache_key(
            endpoint=gated,
            verb="delete",
            vmid=vmid,
            idempotency_key=idempotency_key,
        ),
        lambda: _dispatch_delete(
            endpoint=gated,
            session=session,
            vm_type=vm_type,
            vmid=vmid,
            nb=nb_session,
            idempotency_key=idempotency_key,
            actor=actor,
        ),
    )


async def _handle_backup(
    vm_type: VmType,
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None,
    idempotency_key: str | None,
    actor: str,
    body: BackupRequest | None,
) -> JSONResponse:
    gated = await _gate(
        session,
        endpoint_id,
        writes_disabled_reason=LIFECYCLE_WRITES_DISABLED_REASON,
    )
    if isinstance(gated, JSONResponse):
        return gated
    if body is None or not body.storage:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "reason": "storage_required",
                "detail": "Backup verb requires a JSON body with a non-empty 'storage' field.",
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
    return await _run_with_idempotency_single_flight(
        _idempotency_cache_key(
            endpoint=gated,
            verb="backup",
            vmid=vmid,
            idempotency_key=idempotency_key,
        ),
        lambda: _dispatch_backup(
            endpoint=gated,
            vm_type=vm_type,
            vmid=vmid,
            nb=nb_session,
            idempotency_key=idempotency_key,
            actor=actor,
            storage_name=body.storage,
            mode=body.mode,
            compress=body.compress,
            notes=body.notes,
        ),
    )


async def _handle_delete_snapshot(
    vm_type: VmType,
    vmid: int,
    snapname: str,
    session: SessionDep,
    endpoint_id: int | None,
    idempotency_key: str | None,
    actor: str,
) -> JSONResponse:
    gated = await _gate(
        session,
        endpoint_id,
        writes_disabled_reason=LIFECYCLE_WRITES_DISABLED_REASON,
    )
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
    return await _run_with_idempotency_single_flight(
        _idempotency_cache_key(
            endpoint=gated,
            verb="delete_snapshot",
            vmid=vmid,
            idempotency_key=idempotency_key,
        ),
        lambda: _dispatch_delete_snapshot(
            endpoint=gated,
            vm_type=vm_type,
            vmid=vmid,
            nb=nb_session,
            idempotency_key=idempotency_key,
            actor=actor,
            snapname=snapname,
        ),
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
    return await _run_with_idempotency_single_flight(
        _idempotency_cache_key(
            endpoint=gated,
            verb="migrate",
            vmid=vmid,
            idempotency_key=idempotency_key,
        ),
        lambda: _dispatch_migrate(
            endpoint=gated,
            vm_type=vm_type,
            vmid=vmid,
            nb=nb_session,
            idempotency_key=idempotency_key,
            actor=actor,
            target=body.target,
            online=body.online,
        ),
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

    netbox_vm_id_or_error = await _resolve_audit_target_or_error(
        nb=nb_session, endpoint=endpoint, vmid=vmid, verb="migrate"
    )
    if isinstance(netbox_vm_id_or_error, JSONResponse):
        return netbox_vm_id_or_error
    netbox_vm_id = netbox_vm_id_or_error
    dispatched_at = utcnow_iso()
    cache = get_idempotency_cache()
    writeahead_journal_entry = await _create_writeahead_journal_or_error(
        nb=nb_session,
        netbox_vm_id=netbox_vm_id,
        verb="migrate",
        vm_type=vm_type,
        vmid=vmid,
        endpoint=endpoint,
        actor=actor,
        dispatched_at=dispatched_at,
        idempotency_key=None,
        cache=cache,
        cache_key=None,
    )
    if isinstance(writeahead_journal_entry, JSONResponse):
        return writeahead_journal_entry

    try:
        await _await_with_interruption_journal(
            cancel_task(proxmox, node, task_upid),
            phase="cancel",
            nb=nb_session,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="migrate",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=None,
            cache=cache,
            cache_key=None,
            proxmox_task_upid=task_upid,
        )
    except ProxmoxAPIError as error:
        return await _audit_and_respond(
            nb=nb_session,
            netbox_vm_id=netbox_vm_id,
            writeahead_journal_entry=writeahead_journal_entry,
            verb="migrate",
            vm_type=vm_type,
            vmid=vmid,
            endpoint=endpoint,
            actor=actor,
            dispatched_at=dispatched_at,
            idempotency_key=None,
            cache=cache,
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
        writeahead_journal_entry=writeahead_journal_entry,
        verb="migrate",
        vm_type=vm_type,
        vmid=vmid,
        endpoint=endpoint,
        actor=actor,
        dispatched_at=dispatched_at,
        idempotency_key=None,
        cache=cache,
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


@router.post("/qemu/{vmid}/reboot")
async def reboot_qemu(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> JSONResponse:
    return await _handle_reboot(
        "qemu", vmid, session, endpoint_id, idempotency_key, _actor_label(actor)
    )


@router.post("/lxc/{vmid}/reboot")
async def reboot_lxc(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> JSONResponse:
    return await _handle_reboot(
        "lxc", vmid, session, endpoint_id, idempotency_key, _actor_label(actor)
    )


@router.delete("/qemu/{vmid}")
async def delete_qemu(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> JSONResponse:
    return await _handle_delete(
        "qemu", vmid, session, endpoint_id, idempotency_key, _actor_label(actor)
    )


@router.delete("/lxc/{vmid}")
async def delete_lxc(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> JSONResponse:
    return await _handle_delete(
        "lxc", vmid, session, endpoint_id, idempotency_key, _actor_label(actor)
    )


@router.post("/qemu/{vmid}/backup")
async def backup_qemu(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
    body: BackupRequest | None = Body(default=None),
) -> JSONResponse:
    return await _handle_backup(
        "qemu", vmid, session, endpoint_id, idempotency_key, _actor_label(actor), body
    )


@router.post("/lxc/{vmid}/backup")
async def backup_lxc(
    vmid: int,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
    body: BackupRequest | None = Body(default=None),
) -> JSONResponse:
    return await _handle_backup(
        "lxc", vmid, session, endpoint_id, idempotency_key, _actor_label(actor), body
    )


@router.delete("/qemu/{vmid}/snapshot/{snapname}")
async def delete_snapshot_qemu(
    vmid: int,
    snapname: str,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> JSONResponse:
    return await _handle_delete_snapshot(
        "qemu", vmid, snapname, session, endpoint_id, idempotency_key, _actor_label(actor)
    )


@router.delete("/lxc/{vmid}/snapshot/{snapname}")
async def delete_snapshot_lxc(
    vmid: int,
    snapname: str,
    session: SessionDep,
    endpoint_id: int | None = Query(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    actor: str | None = Header(default=None, alias="X-Proxbox-Actor"),
) -> JSONResponse:
    return await _handle_delete_snapshot(
        "lxc", vmid, snapname, session, endpoint_id, idempotency_key, _actor_label(actor)
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

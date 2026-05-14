"""Process-local registry for in-flight Proxbox sync operations.

The registry exposes:

- ``register_active_sync`` — an async context manager that records a sync
  while its block runs and removes it on exit (including cancellation and
  exceptions). Wrap the body of a sync handler — or, for streaming endpoints,
  the inside of the ``event_stream()`` generator — so the registration tracks
  the actual lifetime of the work, not just the route handler.
- ``get_active_sync`` / ``is_active`` — read helpers used by the ``GET
  /sync/active`` probe.

The registry is intentionally process-local and memory-only. It is a *soft
probe* (good enough for "is this single API replica currently running a
sync?"), not a distributed lock. When the API runs with multiple uvicorn
workers, each worker maintains its own view; cron/single-exec setups should
treat ``/sync/active`` as advisory and rely on cron interval > sync duration.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

_active_lock = asyncio.Lock()
_active_runs: list[dict[str, str]] = []


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def acquire_active_sync(
    operation_id: str,
    *,
    kind: str = "full-update",
) -> dict[str, str]:
    """Record an in-flight sync and return the registry entry handle.

    Pair with ``release_active_sync`` in a ``try/finally``. Prefer
    ``register_active_sync`` (the ``async with`` form) when the call site can
    accommodate it — both routes share the same underlying storage.
    """
    entry = {
        "id": operation_id,
        "kind": kind,
        "started_at": _utcnow_iso(),
    }
    async with _active_lock:
        _active_runs.append(entry)
    return entry


async def release_active_sync(entry: dict[str, str]) -> None:
    """Remove a previously-acquired registry entry. Safe if already removed."""
    async with _active_lock:
        try:
            _active_runs.remove(entry)
        except ValueError:
            pass


@asynccontextmanager
async def register_active_sync(
    operation_id: str,
    *,
    kind: str = "full-update",
) -> AsyncIterator[None]:
    """Record an in-flight sync for the duration of the ``async with`` block.

    The entry is removed on normal exit, exception, and cancellation. Wrap the
    generator body of a streaming endpoint (not the route handler, which
    returns immediately after constructing the ``StreamingResponse``) so the
    registry reflects the real work lifetime.
    """
    entry = await acquire_active_sync(operation_id, kind=kind)
    try:
        yield
    finally:
        await release_active_sync(entry)


async def get_active_sync() -> dict[str, object]:
    """Return the soft-probe response payload for ``GET /sync/active``.

    Reports the oldest currently-running sync (FIFO) so the probe stays stable
    while a new run starts before the previous one finishes. The full list of
    in-flight runs is returned under ``runs`` for diagnostics.
    """
    async with _active_lock:
        snapshot = list(_active_runs)
    if not snapshot:
        return {
            "active": False,
            "started_at": None,
            "id": None,
            "kind": None,
            "runs": [],
        }
    head = snapshot[0]
    return {
        "active": True,
        "started_at": head["started_at"],
        "id": head["id"],
        "kind": head["kind"],
        "runs": snapshot,
    }


async def is_active() -> bool:
    """Return ``True`` when at least one sync is currently registered."""
    async with _active_lock:
        return bool(_active_runs)


def _reset_for_tests() -> None:
    """Drop all registry state. Test-only; not exported in ``__init__``."""
    _active_runs.clear()

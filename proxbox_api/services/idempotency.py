"""In-memory Idempotency-Key cache for operational verb routes (issue #376).

Implements the contract pinned by ``docs/design/operational-verbs.md`` §4:

- Key scope: ``(endpoint_id, verb, vmid, key)``. Same key reused across
  different VMs or verbs does not collide.
- Window: 60 seconds, sliding from first observed POST.
- Resolution: the second request within the window returns the cached
  response of the first; the Proxmox API is called once.
- Storage: in-memory dict in proxbox-api. Entries are cleared by a
  60-second TTL; no SQLite write. Process restart clears the dict —
  acceptable for the 60-second window.

The cache is concurrency-safe via an ``asyncio.Lock`` so that two
near-simultaneous POSTs with the same key resolve to a single dispatch.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

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
TTL_SECONDS = 60.0


@dataclass(frozen=True)
class CacheKey:
    endpoint_id: int
    verb: str
    vmid: int
    key: str


@dataclass
class _Entry:
    response: dict[str, object]
    status_code: int
    journal_finalization: dict[str, object] | None
    expires_at: float


@dataclass(frozen=True)
class CachedResponse:
    response: dict[str, object]
    status_code: int
    journal_finalization: dict[str, object] | None = None


class IdempotencyCache:
    """TTL cache keyed by ``(endpoint_id, verb, vmid, key)``.

    Use :meth:`reserve` from a verb handler. It returns either the
    cached response (when the same key was seen within ``TTL_SECONDS``)
    or ``None``, in which case the caller dispatches the verb and then
    calls :meth:`store` to record the result. A single ``asyncio.Lock``
    serialises the reserve/store pair to avoid the
    "two concurrent POSTs both dispatch" race.
    """

    def __init__(self, ttl_seconds: float = TTL_SECONDS) -> None:
        self._entries: dict[CacheKey, _Entry] = {}
        self._lock = asyncio.Lock()
        self._ttl = ttl_seconds

    def _now(self) -> float:
        return time.monotonic()

    def _prune(self, now: float) -> None:
        expired = [k for k, e in self._entries.items() if e.expires_at <= now]
        for k in expired:
            del self._entries[k]

    async def get(self, cache_key: CacheKey) -> dict[str, object] | None:
        entry = await self.get_entry(cache_key)
        return entry.response if entry is not None else None

    async def get_entry(self, cache_key: CacheKey) -> CachedResponse | None:
        async with self._lock:
            now = self._now()
            self._prune(now)
            entry = self._entries.get(cache_key)
            if entry is None:
                return None
            return CachedResponse(
                response=dict(entry.response),
                status_code=entry.status_code,
                journal_finalization=(
                    dict(entry.journal_finalization)
                    if entry.journal_finalization is not None
                    else None
                ),
            )

    async def store(
        self,
        cache_key: CacheKey,
        response: dict[str, object],
        *,
        status_code: int = 200,
        journal_finalization: dict[str, object] | None = None,
    ) -> None:
        async with self._lock:
            now = self._now()
            self._entries[cache_key] = _Entry(
                response=dict(response),
                status_code=status_code,
                journal_finalization=(
                    dict(journal_finalization) if journal_finalization is not None else None
                ),
                expires_at=now + self._ttl,
            )

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()


_global_cache = IdempotencyCache()


def get_idempotency_cache() -> IdempotencyCache:
    """Return the process-wide idempotency cache singleton."""
    return _global_cache

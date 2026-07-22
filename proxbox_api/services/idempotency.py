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

The cache is concurrency-safe via a mutex for the entry map plus per-key
single-flight locks so that two near-simultaneous POSTs with the same key
resolve to a single dispatch.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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


@dataclass
class _Flight:
    lock: asyncio.Lock
    users: int = 0


@dataclass(frozen=True)
class CachedResponse:
    response: dict[str, object]
    status_code: int
    journal_finalization: dict[str, object] | None = None


class IdempotencyCache:
    """TTL cache keyed by ``(endpoint_id, verb, vmid, key)``.

    Verb handlers should wrap the cache miss, write-ahead journal create,
    Proxmox dispatch, terminal journal update, and response store in
    :meth:`single_flight` when a caller supplies an Idempotency-Key.
    """

    def __init__(self, ttl_seconds: float = TTL_SECONDS) -> None:
        self._entries: dict[CacheKey, _Entry] = {}
        self._flights: dict[CacheKey, _Flight] = {}
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
            existing = self._entries.get(cache_key)
            new_unfinalized = (
                journal_finalization is not None or response.get("journal_finalized") is False
            )
            existing_finalized = (
                existing is not None
                and existing.journal_finalization is None
                and existing.response.get("journal_finalized") is not False
            )
            if existing_finalized and new_unfinalized:
                return
            self._entries[cache_key] = _Entry(
                response=dict(response),
                status_code=status_code,
                journal_finalization=(
                    dict(journal_finalization) if journal_finalization is not None else None
                ),
                expires_at=now + self._ttl,
            )

    @asynccontextmanager
    async def single_flight(self, cache_key: CacheKey) -> AsyncIterator[None]:
        """Serialize one miss-to-store operation for ``cache_key``.

        The keyed lock is intentionally separate from the cache mutex:
        the mutex protects maps, while this lock lets one request run the
        full write-ahead, Proxmox dispatch, journal-finalize, and cache-store
        sequence. Waiters then re-check the cache under the same keyed guard.
        """
        async with self._lock:
            self._prune(self._now())
            flight = self._flights.get(cache_key)
            if flight is None:
                flight = _Flight(lock=asyncio.Lock())
                self._flights[cache_key] = flight
            flight.users += 1

        await flight.lock.acquire()
        try:
            yield
        finally:
            flight.lock.release()
            async with self._lock:
                flight.users -= 1
                if flight.users == 0 and self._flights.get(cache_key) is flight:
                    del self._flights[cache_key]

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()


_global_cache = IdempotencyCache()


def get_idempotency_cache() -> IdempotencyCache:
    """Return the process-wide idempotency cache singleton."""
    return _global_cache

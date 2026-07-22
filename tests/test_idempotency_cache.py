"""Tests for the operational-verb Idempotency-Key cache (issue #376 sub-PR C).

Pins ``docs/design/operational-verbs.md`` §4:

- Key scope: ``(endpoint_id, verb, vmid, key)``. Different verbs / VMs
  with the same raw key MUST NOT collide.
- Window: 60 seconds. Past TTL, the entry is evicted and the next POST
  dispatches again.
- Concurrency: two near-simultaneous POSTs with the same key resolve to
  one cached response.
"""

from __future__ import annotations

import asyncio

import pytest

from proxbox_api.services.idempotency import (
    TTL_SECONDS,
    CacheKey,
    IdempotencyCache,
    get_idempotency_cache,
)


@pytest.fixture
def cache() -> IdempotencyCache:
    return IdempotencyCache()


async def test_get_returns_none_when_unset(cache: IdempotencyCache):
    key = CacheKey(endpoint_id=1, verb="start", vmid=100, key="abc")
    assert await cache.get(key) is None


async def test_store_then_get_returns_response(cache: IdempotencyCache):
    key = CacheKey(endpoint_id=1, verb="start", vmid=100, key="abc")
    await cache.store(key, {"result": "ok", "verb": "start"})
    cached = await cache.get(key)
    assert cached == {"result": "ok", "verb": "start"}


async def test_single_flight_serializes_same_key_and_cleans_up(cache: IdempotencyCache):
    key = CacheKey(endpoint_id=1, verb="start", vmid=100, key="abc")
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def _worker(name: str) -> None:
        async with cache.single_flight(key):
            order.append(name)
            if name == "first":
                first_entered.set()
                await release_first.wait()

    first = asyncio.create_task(_worker("first"))
    await asyncio.wait_for(first_entered.wait(), timeout=1)
    second = asyncio.create_task(_worker("second"))
    await asyncio.sleep(0)
    assert order == ["first"]

    release_first.set()
    await asyncio.gather(first, second)
    assert order == ["first", "second"]
    assert cache._flights == {}


async def test_single_flight_cancelled_waiter_cleans_up_refcount(cache: IdempotencyCache):
    key = CacheKey(endpoint_id=1, verb="start", vmid=100, key="cancelled-waiter")
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def _holder() -> None:
        async with cache.single_flight(key):
            first_entered.set()
            await release_first.wait()

    async def _waiter() -> None:
        async with cache.single_flight(key):
            raise AssertionError("cancelled waiter must not enter the flight")

    async def _wait_for_users(expected: int) -> None:
        for _ in range(20):
            async with cache._lock:
                flight = cache._flights.get(key)
                users = flight.users if flight is not None else 0
            if users == expected:
                return
            await asyncio.sleep(0)
        pytest.fail(f"single-flight users did not reach {expected}")

    holder = asyncio.create_task(_holder())
    await asyncio.wait_for(first_entered.wait(), timeout=1)
    waiter = asyncio.create_task(_waiter())
    await asyncio.wait_for(_wait_for_users(2), timeout=1)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.wait_for(_wait_for_users(1), timeout=1)

    release_first.set()
    await holder
    assert cache._flights == {}


async def test_store_returns_independent_copy(cache: IdempotencyCache):
    """Mutating the returned dict must not corrupt the cache entry."""
    key = CacheKey(endpoint_id=1, verb="start", vmid=100, key="abc")
    await cache.store(key, {"result": "ok"})
    cached = await cache.get(key)
    assert cached is not None
    cached["result"] = "tampered"
    refetched = await cache.get(key)
    assert refetched == {"result": "ok"}


async def test_different_verbs_do_not_collide(cache: IdempotencyCache):
    start_key = CacheKey(endpoint_id=1, verb="start", vmid=100, key="shared")
    stop_key = CacheKey(endpoint_id=1, verb="stop", vmid=100, key="shared")
    await cache.store(start_key, {"verb": "start"})
    await cache.store(stop_key, {"verb": "stop"})
    assert (await cache.get(start_key)) == {"verb": "start"}
    assert (await cache.get(stop_key)) == {"verb": "stop"}


async def test_different_vmids_do_not_collide(cache: IdempotencyCache):
    a = CacheKey(endpoint_id=1, verb="start", vmid=100, key="shared")
    b = CacheKey(endpoint_id=1, verb="start", vmid=101, key="shared")
    await cache.store(a, {"vmid": 100})
    await cache.store(b, {"vmid": 101})
    assert (await cache.get(a)) == {"vmid": 100}
    assert (await cache.get(b)) == {"vmid": 101}


async def test_different_endpoints_do_not_collide(cache: IdempotencyCache):
    a = CacheKey(endpoint_id=1, verb="start", vmid=100, key="shared")
    b = CacheKey(endpoint_id=2, verb="start", vmid=100, key="shared")
    await cache.store(a, {"endpoint_id": 1})
    await cache.store(b, {"endpoint_id": 2})
    assert (await cache.get(a)) == {"endpoint_id": 1}
    assert (await cache.get(b)) == {"endpoint_id": 2}


async def test_store_does_not_regress_finalized_journal_entry(cache: IdempotencyCache):
    key = CacheKey(endpoint_id=1, verb="start", vmid=100, key="abc")
    await cache.store(key, {"result": "ok"}, status_code=200)
    await cache.store(
        key,
        {
            "result": "ok",
            "journal_finalized": False,
            "finalization_error": "netbox patch down",
        },
        status_code=502,
        journal_finalization={"journal_entry_id": 789},
    )

    cached = await cache.get_entry(key)
    assert cached is not None
    assert cached.status_code == 200
    assert cached.response == {"result": "ok"}
    assert cached.journal_finalization is None


async def test_entry_expires_after_ttl():
    """Past TTL, the next ``get`` returns ``None`` (entry evicted)."""
    cache = IdempotencyCache(ttl_seconds=0.05)
    key = CacheKey(endpoint_id=1, verb="start", vmid=100, key="abc")
    await cache.store(key, {"result": "ok"})
    assert (await cache.get(key)) == {"result": "ok"}
    await asyncio.sleep(0.1)
    assert (await cache.get(key)) is None


async def test_default_ttl_is_sixty_seconds():
    """The design doc §4 pins the window at 60 seconds."""
    assert TTL_SECONDS == 60.0


async def test_singleton_returns_same_instance():
    a = get_idempotency_cache()
    b = get_idempotency_cache()
    assert a is b


async def test_clear_evicts_all_entries(cache: IdempotencyCache):
    await cache.store(CacheKey(endpoint_id=1, verb="start", vmid=100, key="a"), {"x": 1})
    await cache.store(CacheKey(endpoint_id=1, verb="stop", vmid=100, key="b"), {"x": 2})
    await cache.clear()
    assert await cache.get(CacheKey(endpoint_id=1, verb="start", vmid=100, key="a")) is None
    assert await cache.get(CacheKey(endpoint_id=1, verb="stop", vmid=100, key="b")) is None

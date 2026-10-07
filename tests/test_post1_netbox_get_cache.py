"""NetBox GET cache identity, invalidation, race, and copy-depth guarantees."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from netbox_sdk.client import ApiResponse

from proxbox_api import netbox_rest
from proxbox_api.netbox_rest import (
    _cache_key,
    _netbox_get_cache,
    clear_rest_get_cache_for_path,
    purge_get_cache_for_api,
    rest_create_async,
    rest_first_async,
    rest_list_async,
)

PATH = "/api/dcim/sites/"


class _Api:
    def __init__(self, rows):
        self.rows = rows
        self.gets = 0
        self.posts = 0
        self.client = SimpleNamespace(request=self._request)

    async def _request(self, method, path, **kwargs):
        if method == "GET":
            self.gets += 1
            rows = [dict(r) for r in self.rows]
            return ApiResponse(
                status=200, text=json.dumps({"count": len(rows), "next": None, "results": rows})
            )
        raise AssertionError(method)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    netbox_rest._reset_netbox_globals()
    monkeypatch.setenv("PROXBOX_NETBOX_GET_CACHE_TTL", "60")
    monkeypatch.setattr(netbox_rest, "_resolve_get_cache_ttl_seconds", lambda: 60.0)
    yield
    netbox_rest._reset_netbox_globals()


def _ids(records):
    return [r.get("id") for r in records]


async def test_distinct_api_objects_never_share_entries_even_if_ids_collide(monkeypatch):
    a = _Api([{"id": 1, "name": "old"}])
    b = _Api([{"id": 2, "name": "new"}])
    # Simulate id() reuse: every object reports the same id.
    with monkeypatch.context() as m:
        m.setattr("builtins.id", lambda obj: 4242)
        assert _ids(await rest_list_async(a, PATH)) == [1]
        assert _ids(await rest_list_async(b, PATH)) == [2]
    assert _cache_key(a, PATH, None) != _cache_key(b, PATH, None)


async def test_purge_drops_entries_for_api():
    a = _Api([{"id": 1}])
    await rest_list_async(a, PATH)
    assert len(_netbox_get_cache) == 1
    assert purge_get_cache_for_api(a) == 1
    assert len(_netbox_get_cache) == 0
    await rest_list_async(a, PATH)
    assert a.gets == 2


async def test_closing_retired_clients_purges_cache():
    from proxbox_api.session import netbox as session_netbox

    class _Client:
        async def close(self):
            return None

    a = _Api([{"id": 1}])
    await rest_list_async(a, PATH)
    a.client.close = _Client().close
    assert len(_netbox_get_cache) == 1
    await session_netbox._close_cached_apis([a])
    assert len(_netbox_get_cache) == 0


async def test_lost_response_retry_ignores_cached_empty_lookup(monkeypatch):
    lookup = {"slug": "x"}
    state = {"created": False, "posts": 0}

    class Api:
        def __init__(self):
            self.client = SimpleNamespace(request=self.request)

        async def request(self, method, path, **kwargs):
            if method == "GET":
                rows = [{"id": 9, "slug": "x"}] if state["created"] else []
                return ApiResponse(
                    status=200,
                    text=json.dumps({"count": len(rows), "next": None, "results": rows}),
                )
            state["posts"] += 1
            state["created"] = True  # the write landed but the response is lost
            raise RuntimeError("Server disconnected")

    monkeypatch.setattr(netbox_rest, "_resolve_netbox_max_retries", lambda: 2)
    monkeypatch.setattr(netbox_rest, "_resolve_netbox_retry_delay", lambda: 0.0)
    api = Api()
    # Prime a cached empty result for the exact lookup query.
    assert await rest_first_async(api, PATH, query={**lookup, "limit": 2}) is None
    record = await rest_create_async(api, PATH, {"slug": "x"}, lookup=lookup)
    assert record.get("id") == 9
    assert state["posts"] == 1


async def test_stale_write_back_after_concurrent_invalidation_is_skipped():
    release = asyncio.Event()
    started = asyncio.Event()

    class SlowApi(_Api):
        async def _request(self, method, path, **kwargs):
            response = await super()._request(method, path, **kwargs)
            started.set()
            await release.wait()
            return response

    api = SlowApi([{"id": 1, "name": "pre-write"}])
    task = asyncio.create_task(rest_list_async(api, PATH))
    await started.wait()
    clear_rest_get_cache_for_path(api, PATH)  # a concurrent write invalidates
    release.set()
    await task
    assert len(_netbox_get_cache) == 0


async def test_unraced_traversal_still_caches():
    api = _Api([{"id": 1}])
    await rest_list_async(api, PATH)
    await rest_list_async(api, PATH)
    assert api.gets == 1


async def test_nested_mutation_does_not_corrupt_cache():
    api = _Api([{"id": 1, "custom_fields": {"a": 1}, "tags": [{"slug": "t"}]}])
    first = await rest_list_async(api, PATH)
    first[0].get("custom_fields")["a"] = 99
    first[0].get("tags").append({"slug": "evil"})
    second = await rest_list_async(api, PATH)  # served from cache
    assert api.gets == 1
    assert second[0].get("custom_fields") == {"a": 1}
    assert second[0].get("tags") == [{"slug": "t"}]
    second[0].get("tags").append({"slug": "evil2"})
    third = await rest_list_async(api, PATH)
    assert third[0].get("tags") == [{"slug": "t"}]

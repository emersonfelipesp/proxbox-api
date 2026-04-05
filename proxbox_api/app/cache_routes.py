"""Root-level cache inspection and reset routes."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from proxbox_api.cache import global_cache
from proxbox_api.netbox_rest import (
    _netbox_get_cache,
    clear_rest_get_cache,
    get_cache_metrics,
    get_cache_prometheus_metrics,
)

cache_router = APIRouter()


@cache_router.get("/cache")
async def get_cache() -> dict:
    netbox_metrics = get_cache_metrics()
    sample_keys = [
        {"api_id": key[0], "path": key[1], "query": key[2]}
        for key in list(_netbox_get_cache.keys())[:20]
    ]
    return {
        "proxbox_cache": global_cache.return_cache(),
        "netbox_get_cache_metrics": netbox_metrics,
        "netbox_get_cache_sample": sample_keys,
    }


@cache_router.get("/cache/metrics")
async def get_cache_metrics_json() -> dict:
    return get_cache_metrics()


@cache_router.get("/cache/metrics/prometheus")
async def get_cache_metrics_prometheus() -> PlainTextResponse:
    return PlainTextResponse(
        content=get_cache_prometheus_metrics(),
        media_type="text/plain; charset=utf-8",
    )


@cache_router.get("/clear-cache")
async def clear_cache() -> dict:
    global_cache.clear_cache()
    clear_rest_get_cache()
    return {"message": "All caches cleared"}


def register_cache_routes(app) -> None:
    """Mount cache routes on the root application."""
    app.include_router(cache_router)

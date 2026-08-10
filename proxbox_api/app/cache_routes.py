"""Root-level cache inspection and reset routes."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse
from sqlmodel import Session

from proxbox_api import database
from proxbox_api.cache import global_cache
from proxbox_api.netbox_rest import (
    _netbox_get_cache,
    clear_rest_get_cache,
    get_cache_metrics,
    get_cache_prometheus_metrics,
)
from proxbox_api.services.auth_lockout import (
    get_auth_lockout_metrics,
    get_auth_lockout_prometheus_metrics,
)
from proxbox_api.services.custom_fields import invalidate_custom_fields_cache
from proxbox_api.services.sync.reconciliation.metrics import (
    get_reconciliation_metrics,
    get_reconciliation_prometheus_metrics,
)

cache_router = APIRouter()


@cache_router.get("/cache")
async def get_cache() -> dict:
    netbox_metrics = get_cache_metrics()
    reconciliation_metrics = get_reconciliation_metrics()
    with Session(database.get_engine()) as session:
        auth_metrics = get_auth_lockout_metrics(session)
    sample_keys = [
        {"api_id": key[0], "path": key[1], "query": key[2]}
        for key in list(_netbox_get_cache.keys())[:20]
    ]
    return {
        "proxbox_cache": global_cache.return_cache(),
        "netbox_get_cache_metrics": netbox_metrics,
        "reconciliation_metrics": reconciliation_metrics,
        "auth_lockout_metrics": auth_metrics,
        "netbox_get_cache_sample": sample_keys,
    }


@cache_router.get("/cache/metrics")
async def get_cache_metrics_json() -> dict:
    with Session(database.get_engine()) as session:
        auth_metrics = get_auth_lockout_metrics(session)
    return {**get_cache_metrics(), **get_reconciliation_metrics(), **auth_metrics}


@cache_router.get("/cache/metrics/prometheus")
async def get_cache_metrics_prometheus() -> PlainTextResponse:
    with Session(database.get_engine()) as session:
        auth_metrics = get_auth_lockout_prometheus_metrics(session)
    return PlainTextResponse(
        content=(
            get_cache_prometheus_metrics() + get_reconciliation_prometheus_metrics() + auth_metrics
        ),
        media_type="text/plain; charset=utf-8",
    )


@cache_router.get("/clear-cache")
async def clear_cache() -> dict:
    global_cache.clear_cache()
    clear_rest_get_cache()
    invalidate_custom_fields_cache()
    return {"message": "All caches cleared"}


def register_cache_routes(app) -> None:
    """Mount cache routes on the root application."""
    app.include_router(cache_router)

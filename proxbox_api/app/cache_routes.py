"""Root-level cache inspection and reset routes."""

from __future__ import annotations

from fastapi import APIRouter

from proxbox_api.cache import global_cache

cache_router = APIRouter()


@cache_router.get("/cache")
async def get_cache() -> dict:
    return global_cache.return_cache()


@cache_router.get("/clear-cache")
async def clear_cache() -> dict:
    global_cache.clear_cache()
    return {"message": "Cache cleared"}


def register_cache_routes(app) -> None:
    """Mount cache routes on the root application."""
    app.include_router(cache_router)

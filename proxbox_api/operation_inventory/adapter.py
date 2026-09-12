"""Fail-closed adapter for the reviewed FastAPI lazy-router implementation."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Literal

from fastapi.routing import RouteContext, iter_route_contexts
from starlette.routing import BaseRoute
from starlette.routing import Mount as ASGIMount

from .provenance import callable_identity
from .schema import InventoryError, Mount, Operation

SUPPORTED = {"fastapi": "0.137.2", "starlette": "1.3.1"}
type RouteProtocol = Literal["http", "websocket", "mount"]


def require_framework() -> None:
    """Unknown framework semantics require an explicit adapter review."""
    for name, expected in SUPPORTED.items():
        if importlib.metadata.version(name) != expected:
            raise InventoryError(f"Unsupported routing framework: {name}")


def concrete(context: RouteContext) -> object:
    """Prefixed WebSocket/Mount routes live in the pinned private context."""
    effective = getattr(context, "_route_context", None)
    return getattr(effective, "starlette_route", None) or context


def _protocol(original: object) -> RouteProtocol:
    kind = type(original).__name__
    protocols: dict[str, RouteProtocol] = {
        "APIRoute": "http",
        "Route": "http",
        "APIWebSocketRoute": "websocket",
        "WebSocketRoute": "websocket",
        "Mount": "mount",
    }
    if kind not in protocols:
        raise InventoryError(f"Unsupported route kind: {kind}")
    return protocols[kind]


def _operation(context: RouteContext, root: Path, prefix: str, mounts: list[Mount]) -> Operation:
    route = concrete(context)
    original = context.original_route
    protocol = _protocol(original)
    endpoint = getattr(route, "app" if protocol == "mount" else "endpoint")
    path = getattr(route, "path")
    if type(path) is not str:
        raise InventoryError("Route has no effective path")
    return Operation(
        protocol=protocol,
        path=prefix + path,
        declared_path=getattr(original, "path") or "/",
        methods=sorted(getattr(route, "methods") or []) if protocol == "http" else [],
        route_kind=type(original).__name__,
        name=getattr(route, "name") or type(endpoint).__qualname__,
        mounts=mounts,
        handler=callable_identity(endpoint, root),
        generated=None,
    )


def walk(
    routes: Sequence[BaseRoute], root: Path, prefix: str = "", mounts: list[Mount] | None = None
) -> Iterator[Operation]:
    """Preserve every lazy registration and recurse explicit ASGI mounts in order."""
    require_framework()
    chain = mounts or []
    for context in iter_route_contexts(routes):
        operation = _operation(context, root, prefix, chain)
        yield operation
        route = concrete(context)
        if isinstance(context.original_route, ASGIMount):
            children = getattr(route, "routes")
            nested = [*chain, Mount(path=operation.path, name=getattr(route, "name"))]
            yield from walk(children, root, operation.path, nested)


def collisions(operations: list[Operation]) -> list[dict[str, object]]:
    """Report exact overlaps only; parameterized semantic shadowing is not proven."""
    groups: dict[tuple[str, str, str], list[int]] = {}
    for index, operation in enumerate(operations):
        for method in operation.methods or [""]:
            key = (operation.protocol, operation.path, method)
            groups.setdefault(key, []).append(index)
    return [
        {"protocol": key[0], "path": key[1], "method": key[2], "registrations": rows}
        for key, rows in groups.items()
        if len(rows) > 1
    ]

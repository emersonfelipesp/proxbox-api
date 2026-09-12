"""Fixed nested routing oracles independent of extractor output."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI, WebSocket
from starlette.routing import Host

from proxbox_api.operation_inventory.adapter import collisions, walk
from proxbox_api.operation_inventory.schema import InventoryError

ROOT = Path(__file__).absolute().parents[2]


def test_route_without_effective_path_is_not_an_inventory_record(monkeypatch):
    from fastapi.routing import APIRoute

    from proxbox_api.operation_inventory import adapter

    async def never_called():
        raise AssertionError("Inventory must not invoke a route")

    original = APIRoute("/fixed", never_called)
    context = SimpleNamespace(original_route=original)
    monkeypatch.setattr(
        adapter, "concrete", lambda _: SimpleNamespace(path=None, endpoint=never_called)
    )
    with pytest.raises(ValueError, match="effective path"):
        adapter._operation(context, Path(__file__).absolute().parents[2], "", [])


def application():
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    leaf = APIRouter()

    @leaf.api_route("/item/{item_id}", methods=["GET", "DELETE"])
    def item(item_id: str):
        raise AssertionError("Discovery must not invoke HTTP handlers")

    @leaf.websocket("/socket/{channel}")
    async def socket(websocket: WebSocket, channel: str):
        raise AssertionError("Discovery must not invoke WebSocket handlers")

    parent = APIRouter()
    parent.include_router(leaf, prefix="/inner")
    app.include_router(parent, prefix="/outer")
    app.include_router(parent, prefix="/outer")
    child = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    child.include_router(leaf, prefix="/nested")
    app.mount("/mounted", child, name="child")
    return app


def test_exact_nested_http_websocket_mount_and_collision_oracle():
    rows = list(walk(application().routes, ROOT))
    assert [(row.protocol, row.path, row.methods) for row in rows] == [
        ("http", "/outer/inner/item/{item_id}", ["DELETE", "GET"]),
        ("websocket", "/outer/inner/socket/{channel}", []),
        ("http", "/outer/inner/item/{item_id}", ["DELETE", "GET"]),
        ("websocket", "/outer/inner/socket/{channel}", []),
        ("mount", "/mounted", []),
        ("http", "/mounted/nested/item/{item_id}", ["DELETE", "GET"]),
        ("websocket", "/mounted/nested/socket/{channel}", []),
    ]
    assert [entry["registrations"] for entry in collisions(rows)] == [[0, 2], [0, 2], [1, 3]]
    assert rows[-1].mounts[0].path == "/mounted"
    assert rows[-1].mounts[0].name == "child"
    assert rows[0].declared_path == "/item/{item_id}"
    assert rows[1].handler.qualname == "application.<locals>.socket"


def test_wrong_framework_support_fails(monkeypatch):
    monkeypatch.setattr("importlib.metadata.version", lambda _: "0.0.0")
    with pytest.raises(InventoryError, match="Unsupported routing framework"):
        list(walk(application().routes, ROOT))


def test_unknown_route_kind_fails():
    app = FastAPI()
    with pytest.raises(InventoryError, match="Unsupported route kind"):
        list(walk([Host("example.invalid", app=app)], ROOT))


def test_omitting_websockets_breaks_fixed_oracle(monkeypatch):
    from proxbox_api.operation_inventory import adapter

    original = adapter.iter_route_contexts

    def omitted(routes):
        return (
            row for row in original(routes) if "WebSocket" not in type(row.original_route).__name__
        )

    monkeypatch.setattr(adapter, "iter_route_contexts", omitted)
    with pytest.raises(AssertionError):
        test_exact_nested_http_websocket_mount_and_collision_oracle()


def test_losing_prefixed_websocket_context_breaks_fixed_oracle(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.operation_inventory.adapter.concrete", lambda row: row.original_route
    )
    with pytest.raises(AssertionError):
        test_exact_nested_http_websocket_mount_and_collision_oracle()

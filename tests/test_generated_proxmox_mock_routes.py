from __future__ import annotations

import os
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from proxbox_api.mock.routes import register_generated_proxmox_mock_routes
from proxbox_api.mock.state import reset_shared_mock_state
from proxbox_api.proxmox_to_netbox.proxmox_schema import load_proxmox_generated_openapi

TEST_MOCK_OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "Test Proxmox Mock", "version": "test-mock"},
    "servers": [{"url": "/api2/json"}],
    "paths": {
        "/items": {
            "get": {
                "operationId": "get_items",
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "item_id": {"type": "string"},
                                            "name": {"type": "string"},
                                            "status": {"type": "string"},
                                        },
                                        "required": ["item_id", "name", "status"],
                                    },
                                }
                            }
                        },
                    }
                },
            },
            "post": {
                "operationId": "post_items",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "item_id": {"type": "string"},
                                    "name": {"type": "string"},
                                    "status": {"type": "string"},
                                },
                                "required": ["item_id", "name", "status"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "item_id": {"type": "string"},
                                        "name": {"type": "string"},
                                        "status": {"type": "string"},
                                    },
                                    "required": ["item_id", "name", "status"],
                                }
                            }
                        },
                    }
                },
            },
        },
        "/items/{item_id}": {
            "get": {
                "operationId": "get_items_item_id",
                "parameters": [
                    {
                        "name": "item_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "item_id": {"type": "string"},
                                        "name": {"type": "string"},
                                        "status": {"type": "string"},
                                    },
                                    "required": ["item_id", "name", "status"],
                                }
                            }
                        },
                    }
                },
            },
            "put": {
                "operationId": "put_items_item_id",
                "parameters": [
                    {
                        "name": "item_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "status": {"type": "string"},
                                },
                                "required": ["name", "status"],
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "item_id": {"type": "string"},
                                        "name": {"type": "string"},
                                        "status": {"type": "string"},
                                    },
                                    "required": ["item_id", "name", "status"],
                                }
                            }
                        },
                    }
                },
            },
            "patch": {
                "operationId": "patch_items_item_id",
                "parameters": [
                    {
                        "name": "item_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "status": {"type": "string"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "item_id": {"type": "string"},
                                        "name": {"type": "string"},
                                        "status": {"type": "string"},
                                    },
                                    "required": ["item_id", "name", "status"],
                                }
                            }
                        },
                    }
                },
            },
            "delete": {
                "operationId": "delete_items_item_id",
                "parameters": [
                    {
                        "name": "item_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "item_id": {"type": "string"},
                                        "name": {"type": "string"},
                                        "status": {"type": "string"},
                                    },
                                    "required": ["item_id", "name", "status"],
                                }
                            }
                        },
                    }
                },
            },
        },
    },
}


def _build_test_app(*, namespace: str, owner_pid: int) -> FastAPI:
    app = FastAPI()
    register_generated_proxmox_mock_routes(
        app,
        openapi_document=TEST_MOCK_OPENAPI,
        namespace=namespace,
        owner_pid=owner_pid,
    )
    return app


@pytest.fixture
def mock_namespace() -> str:
    namespace = f"pytest-{uuid4().hex}"
    yield namespace
    reset_shared_mock_state(namespace=namespace, owner_pid=os.getpid())


@pytest.mark.asyncio
async def test_generated_proxmox_mock_routes_cover_real_schema():
    app = FastAPI()
    document = load_proxmox_generated_openapi()
    state = register_generated_proxmox_mock_routes(
        app,
        openapi_document=document,
        namespace=f"coverage-{uuid4().hex}",
        owner_pid=os.getpid(),
    )

    expected_count = sum(
        1
        for path_item in (document.get("paths") or {}).values()
        if isinstance(path_item, dict)
        for method in path_item
        if method.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    )
    generated_routes = [
        route for route in app.routes if getattr(route, "path", "").startswith("/api2/json/")
    ]

    assert state["route_count"] == expected_count
    assert len(generated_routes) == expected_count
    assert "/api2/json/version" in app.openapi()["paths"]


@pytest.mark.asyncio
async def test_generated_proxmox_mock_routes_support_mutation_and_reads(mock_namespace: str):
    app = _build_test_app(namespace=mock_namespace, owner_pid=os.getpid())
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        initial = await client.get("/api2/json/items")
        assert initial.status_code == 200
        assert len(initial.json()) == 1

        created = await client.post(
            "/api2/json/items",
            json={"item_id": "alpha", "name": "Alpha", "status": "new"},
        )
        assert created.status_code == 200
        assert created.json()["item_id"] == "alpha"

        listed = await client.get("/api2/json/items")
        item_ids = {item["item_id"] for item in listed.json()}
        assert "alpha" in item_ids

        fetched = await client.get("/api2/json/items/alpha")
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "Alpha"

        replaced = await client.put(
            "/api2/json/items/alpha",
            json={"name": "Alpha v2", "status": "ready"},
        )
        assert replaced.status_code == 200
        assert replaced.json()["name"] == "Alpha v2"

        patched = await client.patch(
            "/api2/json/items/alpha",
            json={"status": "done"},
        )
        assert patched.status_code == 200
        assert patched.json()["status"] == "done"

        fetched_again = await client.get("/api2/json/items/alpha")
        assert fetched_again.status_code == 200
        assert fetched_again.json()["name"] == "Alpha v2"
        assert fetched_again.json()["status"] == "done"

        deleted = await client.delete("/api2/json/items/alpha")
        assert deleted.status_code == 200
        assert deleted.json()["item_id"] == "alpha"

        missing = await client.get("/api2/json/items/alpha")
        assert missing.status_code == 404

        listed_after_delete = await client.get("/api2/json/items")
        item_ids_after_delete = {item["item_id"] for item in listed_after_delete.json()}
        assert "alpha" not in item_ids_after_delete


@pytest.mark.asyncio
async def test_generated_proxmox_mock_routes_preserve_state_across_rebuild(mock_namespace: str):
    first_app = _build_test_app(namespace=mock_namespace, owner_pid=os.getpid())
    first_transport = httpx.ASGITransport(app=first_app)

    async with httpx.AsyncClient(transport=first_transport, base_url="http://test") as client:
        created = await client.post(
            "/api2/json/items",
            json={"item_id": "reload-item", "name": "Reload", "status": "hot"},
        )
        assert created.status_code == 200

    reloaded_app = _build_test_app(namespace=mock_namespace, owner_pid=os.getpid())
    reloaded_transport = httpx.ASGITransport(app=reloaded_app)
    async with httpx.AsyncClient(transport=reloaded_transport, base_url="http://test") as client:
        fetched = await client.get("/api2/json/items/reload-item")
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "Reload"
        assert fetched.json()["status"] == "hot"

    reset_shared_mock_state(namespace=mock_namespace, owner_pid=os.getpid())

    reset_app = _build_test_app(namespace=mock_namespace, owner_pid=os.getpid())
    reset_transport = httpx.ASGITransport(app=reset_app)
    async with httpx.AsyncClient(transport=reset_transport, base_url="http://test") as client:
        fetched_after_reset = await client.get("/api2/json/items/reload-item")
        assert fetched_after_reset.status_code == 200
        assert fetched_after_reset.json()["name"] != "Reload"

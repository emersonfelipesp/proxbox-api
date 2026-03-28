from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlmodel import Session

from proxbox_api.database import ProxmoxEndpoint, get_session
from proxbox_api.main import app
from proxbox_api.proxmox_to_netbox.proxmox_schema import (
    available_proxmox_openapi_versions,
)
from proxbox_api.routes.proxmox.runtime_generated import (
    clear_generated_proxmox_routes,
    generated_proxmox_route_state,
    register_generated_proxmox_routes,
)
from proxbox_api.routes.proxmox.viewer_codegen import refresh_generated_proxmox_routes

TEST_GENERATED_OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "Test Proxmox", "version": "test-generated"},
    "paths": {
        "/cluster/resources": {
            "get": {
                "operationId": "get_cluster_resources",
                "summary": "List cluster resources",
                "parameters": [
                    {
                        "name": "type",
                        "in": "query",
                        "required": False,
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
                                        "path": {"type": "string"},
                                        "method": {"type": "string"},
                                        "params": {"type": "object"},
                                    },
                                    "required": ["path", "method"],
                                }
                            }
                        },
                    }
                },
            }
        },
        "/access/acl": {
            "post": {
                "operationId": "post_access_acl",
                "summary": "Update ACL",
                "description": "Updates ACL entries.\n\n## Usage\npvesh set /access/acl --path /vms --roles PVEAdmin",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "roles": {"type": "string"},
                                    "groups-autocreate": {"type": "boolean"},
                                },
                                "required": ["path", "roles"],
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
                                        "path": {"type": "string"},
                                        "method": {"type": "string"},
                                        "payload": {"type": "object"},
                                    },
                                    "required": ["path", "method"],
                                }
                            }
                        },
                    }
                },
            }
        },
        "/nodes/{node}/qemu/{vmid}/config": {
            "get": {
                "operationId": "get_nodes_node_qemu_vmid_config",
                "summary": "Read VM config",
                "parameters": [
                    {
                        "name": "node",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "vmid",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "integer"},
                    },
                    {
                        "name": "current",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "boolean"},
                    },
                ],
                "responses": {
                    "200": {
                        "description": "ok",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"},
                                        "method": {"type": "string"},
                                        "params": {"type": "object"},
                                    },
                                    "required": ["path", "method"],
                                }
                            }
                        },
                    }
                },
            }
        },
    },
}

TEST_GENERATED_OPENAPI_V83 = {
    **TEST_GENERATED_OPENAPI,
    "info": {"title": "Test Proxmox", "version": "8.3.0-generated"},
}


class ProxyFakeResource:
    def __init__(self, api, path):
        self.api = api
        self.path = path

    def get(self, **kwargs):
        if self.path == "cluster/status":
            return [
                {"type": "cluster", "name": "lab-cluster"},
                {"type": "node", "name": "pve01"},
            ]
        if self.path == "cluster/config/join":
            return {"nodelist": [{"pve_fp": "fingerprint"}]}
        self.api.calls.append(("GET", self.path, kwargs))
        return {"path": self.path, "method": "GET", "params": kwargs}

    def post(self, **kwargs):
        self.api.calls.append(("POST", self.path, kwargs))
        return {"path": self.path, "method": "POST", "payload": kwargs}


class ProxyFakeProxmoxAPI:
    instances = []

    def __init__(self, host, **kwargs):
        self.host = host
        self.kwargs = kwargs
        self.calls = []
        self.version = SimpleNamespace(get=lambda: {"version": "8.3.0"})
        type(self).instances.append(self)

    def __call__(self, path):
        return ProxyFakeResource(self, path)


def _override_db_session(db_engine):
    def override_get_session():
        with Session(db_engine) as session:
            yield session

    return override_get_session


def test_generated_routes_appear_in_openapi():
    register_generated_proxmox_routes(
        app,
        openapi_documents={
            "latest": TEST_GENERATED_OPENAPI,
            "8.3.0": TEST_GENERATED_OPENAPI_V83,
        },
    )

    schema = app.openapi()
    assert "/proxmox/api2/latest/cluster/resources" in schema["paths"]
    assert "/proxmox/api2/8.3.0/nodes/{node}/qemu/{vmid}/config" in schema["paths"]
    assert "/proxmox/api2/access/acl" in schema["paths"]

    assert list(schema["paths"]).index("/proxmox/api2/latest/cluster/resources") < list(
        schema["paths"]
    ).index("/proxmox/api2/8.3.0/cluster/resources")

    post_acl = schema["paths"]["/proxmox/api2/latest/access/acl"]["post"]
    parameter_names = {parameter["name"] for parameter in post_acl["parameters"]}
    assert {"source", "target_name", "target_domain", "target_ip_address"} <= parameter_names
    assert post_acl["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert "proxmox / live-generated / latest" in post_acl["tags"]
    assert "## Usage" in post_acl["description"]


def test_generated_proxy_route_forwards_request_and_validates_response(
    monkeypatch,
    db_engine,
):
    monkeypatch.setattr("proxbox_api.session.proxmox.ProxmoxAPI", ProxyFakeProxmoxAPI)
    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.runtime_generated.available_proxmox_openapi_versions",
        lambda: ["latest"],
    )
    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.runtime_generated.load_proxmox_generated_openapi",
        lambda version_tag="latest": TEST_GENERATED_OPENAPI,
    )

    with Session(db_engine) as session:
        session.add(
            ProxmoxEndpoint(
                name="pve01",
                ip_address="10.0.0.10",
                domain="pve01.local",
                port=8006,
                username="root@pam",
                password="secret",
                verify_ssl=False,
            )
        )
        session.commit()

    app.dependency_overrides[get_session] = _override_db_session(db_engine)
    register_generated_proxmox_routes(
        app,
        openapi_documents={"latest": TEST_GENERATED_OPENAPI},
    )

    with TestClient(app) as client:
        response = client.post(
            "/proxmox/api2/latest/access/acl",
            json={
                "path": "/vms",
                "roles": "PVEAdmin",
                "groups-autocreate": True,
            },
        )
        alias_response = client.post(
            "/proxmox/api2/access/acl",
            json={
                "path": "/vms",
                "roles": "PVEAdmin",
                "groups-autocreate": True,
            },
        )

    assert response.status_code == 200
    assert alias_response.status_code == 200
    assert response.json()["path"] == "access/acl"
    assert response.json()["payload"]["groups-autocreate"] is True
    assert ProxyFakeProxmoxAPI.instances[-1].calls[-1] == (
        "POST",
        "access/acl",
        {"path": "/vms", "roles": "PVEAdmin", "groups-autocreate": True},
    )


def test_generated_proxy_route_requires_explicit_selector_for_multiple_endpoints(
    monkeypatch,
    db_engine,
):
    monkeypatch.setattr("proxbox_api.session.proxmox.ProxmoxAPI", ProxyFakeProxmoxAPI)
    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.runtime_generated.available_proxmox_openapi_versions",
        lambda: ["latest"],
    )
    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.runtime_generated.load_proxmox_generated_openapi",
        lambda version_tag="latest": TEST_GENERATED_OPENAPI,
    )

    with Session(db_engine) as session:
        session.add(
            ProxmoxEndpoint(
                name="pve01",
                ip_address="10.0.0.10",
                domain="pve01.local",
                port=8006,
                username="root@pam",
                password="secret",
                verify_ssl=False,
            )
        )
        session.add(
            ProxmoxEndpoint(
                name="pve02",
                ip_address="10.0.0.11",
                domain="pve02.local",
                port=8006,
                username="root@pam",
                password="secret",
                verify_ssl=False,
            )
        )
        session.commit()

    app.dependency_overrides[get_session] = _override_db_session(db_engine)
    register_generated_proxmox_routes(
        app,
        openapi_documents={"latest": TEST_GENERATED_OPENAPI},
    )

    with TestClient(app) as client:
        missing_selector = client.get("/proxmox/api2/latest/cluster/resources")
        selected = client.get(
            "/proxmox/api2/latest/cluster/resources",
            params={"target_domain": "pve02.local", "type": "vm"},
        )

    assert missing_selector.status_code == 400
    assert "Multiple Proxmox endpoints configured" in missing_selector.json()["message"]
    assert selected.status_code == 200
    assert selected.json()["params"]["type"] == "vm"


def test_refresh_generated_routes_endpoint_rebuilds_runtime_state(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.runtime_generated.available_proxmox_openapi_versions",
        lambda: ["8.3.0", "latest"],
    )
    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.runtime_generated.load_proxmox_generated_openapi",
        lambda version_tag="latest": (
            TEST_GENERATED_OPENAPI if version_tag == "latest" else TEST_GENERATED_OPENAPI_V83
        ),
    )

    result = asyncio.run(refresh_generated_proxmox_routes(version_tag=None))
    state = generated_proxmox_route_state()

    assert result["route_count"] == 9
    assert result["state"]["mounted_versions"] == ["latest", "8.3.0"]
    assert state["versions"]["latest"]["schema_version"] == "test-generated"
    assert state["versions"]["8.3.0"]["schema_version"] == "8.3.0-generated"
    assert result["cache_source"] == "generated-artifacts"
    assert Path(result["cache_path"]).exists()


def test_refresh_generated_routes_endpoint_rebuilds_single_version(monkeypatch):
    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.runtime_generated.load_proxmox_generated_openapi",
        lambda version_tag="latest": TEST_GENERATED_OPENAPI_V83,
    )

    result = asyncio.run(refresh_generated_proxmox_routes(version_tag="8.3.0"))
    state = generated_proxmox_route_state()

    assert result["route_count"] == 3
    assert result["state"]["mounted_versions"] == ["8.3.0"]
    assert state["versions"]["8.3.0"]["schema_version"] == "8.3.0-generated"
    assert result["cache_source"] == "generated-artifacts"


def test_available_proxmox_openapi_versions_ignores_non_version_entries(tmp_path, monkeypatch):
    generated_root = tmp_path / "generated" / "proxmox"
    latest_dir = generated_root / "latest"
    version_dir = generated_root / "8.3.0"
    ignored_dir = generated_root / "__pycache__"
    empty_dir = generated_root / "scratch"

    latest_dir.mkdir(parents=True)
    version_dir.mkdir()
    ignored_dir.mkdir()
    empty_dir.mkdir()
    (generated_root / "CLAUDE.md").write_text("guide", encoding="utf-8")
    (latest_dir / "openapi.json").write_text("{}", encoding="utf-8")
    (version_dir / "openapi.json").write_text("{}", encoding="utf-8")
    (ignored_dir / "openapi.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.proxmox_schema.proxmox_generated_openapi_root",
        lambda: Path(generated_root),
    )

    assert available_proxmox_openapi_versions() == ["8.3.0", "latest"]


def test_register_generated_routes_uses_persisted_cache_on_reload(tmp_path, monkeypatch):
    generated_root = tmp_path / "generated" / "proxmox"
    cache_path = generated_root / "runtime_generated_routes_cache.json"
    latest_dir = generated_root / "latest"
    latest_dir.mkdir(parents=True)

    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.proxmox_schema.proxmox_generated_openapi_root",
        lambda: Path(generated_root),
    )
    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.runtime_generated.proxmox_generated_route_cache_path",
        lambda: Path(cache_path),
    )
    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.runtime_generated.available_proxmox_openapi_versions",
        lambda: [],
    )
    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.runtime_generated.load_proxmox_generated_openapi",
        lambda version_tag="latest": {},
    )

    register_generated_proxmox_routes(
        app,
        openapi_documents={"latest": TEST_GENERATED_OPENAPI},
    )
    clear_generated_proxmox_routes(app)

    result = register_generated_proxmox_routes(app)
    state = generated_proxmox_route_state()

    assert cache_path.exists()
    assert result["cache_source"] == "runtime-cache"
    assert state["loaded_from_cache"] is True
    assert "/proxmox/api2/latest/cluster/resources" in app.openapi()["paths"]


def test_register_generated_routes_writes_cache_manifest(tmp_path, monkeypatch):
    generated_root = tmp_path / "generated" / "proxmox"
    cache_path = generated_root / "runtime_generated_routes_cache.json"

    monkeypatch.setattr(
        "proxbox_api.proxmox_to_netbox.proxmox_schema.proxmox_generated_openapi_root",
        lambda: Path(generated_root),
    )
    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.runtime_generated.proxmox_generated_route_cache_path",
        lambda: Path(cache_path),
    )

    result = register_generated_proxmox_routes(
        app,
        openapi_documents={
            "latest": TEST_GENERATED_OPENAPI,
            "8.3.0": TEST_GENERATED_OPENAPI_V83,
        },
    )

    payload = json.loads(cache_path.read_text(encoding="utf-8"))

    assert result["cache_path"] == str(cache_path)
    assert payload["mounted_versions"] == ["latest", "8.3.0"]
    assert payload["documents"]["latest"]["info"]["version"] == "test-generated"
    assert payload["documents"]["8.3.0"]["info"]["version"] == "8.3.0-generated"

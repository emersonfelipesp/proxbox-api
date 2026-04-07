from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import Session
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.database import ProxmoxEndpoint, get_async_session, get_session
from proxbox_api.main import app
from proxbox_api.proxmox_codegen.pydantic_generator import (
    generate_pydantic_models_from_openapi,
)
from proxbox_api.proxmox_codegen.utils import pascal_case
from proxbox_api.proxmox_to_netbox.proxmox_schema import (
    DEFAULT_PROXMOX_OPENAPI_TAG,
    available_proxmox_openapi_versions,
    load_proxmox_generated_openapi,
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

CONTROL_QUERY_PARAM_NAMES = {
    "source",
    "target_name",
    "target_domain",
    "target_ip_address",
}
SUPPORTED_GENERATED_METHODS = {"GET", "POST", "PUT", "DELETE"}
GENERATED_MODEL_MODULES: dict[str, ModuleType] = {}


def _resolved_schema(schema: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}
    if isinstance(schema.get("oneOf"), list) and schema["oneOf"]:
        candidate = schema["oneOf"][0]
        if isinstance(candidate, dict):
            return candidate
    return schema


def _sample_value_for_schema(schema: dict[str, Any] | None, *, seed: str) -> Any:  # noqa: C901
    schema = _resolved_schema(schema)
    if not schema:
        return {}

    if "const" in schema:
        return schema["const"]

    schema_type = schema.get("type")
    if schema_type == "null":
        return None
    if schema_type == "string":
        if "default" in schema:
            return str(schema["default"])
        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            return str(enum[0])
        return seed
    if schema_type == "integer":
        if "default" in schema:
            try:
                return int(schema["default"])
            except (TypeError, ValueError):
                pass
        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            try:
                return int(enum[0])
            except (TypeError, ValueError):
                pass
        return 101
    if schema_type == "number":
        if "default" in schema:
            try:
                return float(schema["default"])
            except (TypeError, ValueError):
                pass
        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            try:
                return float(enum[0])
            except (TypeError, ValueError):
                pass
        return 1.5
    if schema_type == "boolean":
        if "default" in schema:
            default = schema["default"]
            if isinstance(default, str):
                if default.lower() in {"0", "false", "no", "off"}:
                    return False
                if default.lower() in {"1", "true", "yes", "on"}:
                    return True
            return bool(default)
        enum = schema.get("enum")
        if isinstance(enum, list) and enum:
            enum_value = enum[0]
            if isinstance(enum_value, str):
                if enum_value.lower() in {"0", "false", "no", "off"}:
                    return False
                if enum_value.lower() in {"1", "true", "yes", "on"}:
                    return True
            return bool(enum_value)
        return True
    if schema_type == "array":
        if "default" in schema and isinstance(schema["default"], list):
            return schema["default"]
        return [
            _sample_value_for_schema(
                schema.get("items") if isinstance(schema.get("items"), dict) else {},
                seed=f"{seed}_item",
            )
        ]
    if schema_type == "object":
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        if not properties:
            return {}
        payload: dict[str, Any] = {}
        for name, property_schema in sorted(properties.items()):
            payload[name] = _sample_value_for_schema(
                property_schema if isinstance(property_schema, dict) else {},
                seed=name,
            )
        return payload
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    return {}


def _render_path(path_template: str, path_values: dict[str, Any]) -> str:
    rendered = path_template
    for name, value in path_values.items():
        rendered = rendered.replace(f"{{{name}}}", str(value))
    return rendered


def _request_body_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    schema = (
        operation.get("requestBody", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )
    return schema if isinstance(schema, dict) else None


def _response_schema(operation: dict[str, Any]) -> dict[str, Any] | None:
    schema = (
        operation.get("responses", {})
        .get("200", {})
        .get("content", {})
        .get("application/json", {})
        .get("schema")
    )
    return schema if isinstance(schema, dict) else None


def _build_request_inputs(openapi_path: str, operation: dict[str, Any]) -> dict[str, Any]:
    path_values: dict[str, Any] = {}
    query_values: dict[str, Any] = {}
    expected_forwarded: dict[str, Any] = {}

    for parameter in operation.get("parameters", []):
        if not isinstance(parameter, dict):
            continue
        parameter_name = parameter.get("name")
        parameter_in = parameter.get("in")
        if not isinstance(parameter_name, str) or parameter_in not in {"path", "query"}:
            continue

        schema = parameter.get("schema") if isinstance(parameter.get("schema"), dict) else {}
        sample = _sample_value_for_schema(schema, seed=parameter_name)
        if parameter_in == "path":
            path_values[parameter_name] = sample
            continue

        request_name = (
            f"op_{parameter_name}"
            if parameter_name in CONTROL_QUERY_PARAM_NAMES
            else parameter_name
        )
        query_values[request_name] = sample
        expected_forwarded[parameter_name] = sample

    body_schema = _request_body_schema(operation)
    path_parameter_names = set(path_values)
    if body_schema is not None and path_parameter_names:
        body_schema = json.loads(json.dumps(body_schema))
        properties = body_schema.get("properties")
        if isinstance(properties, dict):
            body_schema["properties"] = {
                name: value
                for name, value in properties.items()
                if name not in path_parameter_names
            }
        required = body_schema.get("required")
        if isinstance(required, list):
            body_schema["required"] = [
                name for name in required if name not in path_parameter_names
            ]
    request_body = (
        _sample_value_for_schema(body_schema, seed=openapi_path.strip("/").replace("/", "_"))
        if body_schema is not None
        else None
    )
    if isinstance(request_body, dict):
        expected_forwarded.update(request_body)

    return {
        "path_values": path_values,
        "query_values": query_values,
        "request_body": request_body,
        "expected_forwarded": expected_forwarded,
    }


def _build_generated_route_cases() -> list[Any]:
    cases = []
    for version_tag in available_proxmox_openapi_versions():
        document = load_proxmox_generated_openapi(version_tag=version_tag)
        if not document:
            continue
        for openapi_path, path_item in sorted((document.get("paths") or {}).items()):
            if not isinstance(path_item, dict):
                continue
            for method, operation in sorted(path_item.items()):
                method_name = method.upper()
                if method_name not in SUPPORTED_GENERATED_METHODS or not isinstance(
                    operation, dict
                ):
                    continue

                request_inputs = _build_request_inputs(openapi_path, operation)
                rendered_path = _render_path(openapi_path, request_inputs["path_values"])
                shared_case = {
                    "version_tag": version_tag,
                    "openapi_path": openapi_path,
                    "method": method_name,
                    "route_path": f"/proxmox/api2/{version_tag}{rendered_path}",
                    "upstream_path": rendered_path.lstrip("/"),
                    **request_inputs,
                }
                cases.append(
                    pytest.param(
                        shared_case,
                        id=f"{version_tag}:{method_name}:{openapi_path}",
                    )
                )

                if version_tag == DEFAULT_PROXMOX_OPENAPI_TAG:
                    alias_case = {
                        **shared_case,
                        "route_path": f"/proxmox/api2{rendered_path}",
                        "alias": True,
                    }
                    cases.append(
                        pytest.param(
                            alias_case,
                            id=f"alias:{method_name}:{openapi_path}",
                        )
                    )

    return cases


GENERATED_ROUTE_CASES = _build_generated_route_cases()


@pytest.fixture(scope="module", autouse=True)
def _isolate_runtime_generated_route_cache(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Use a worker-local cache file so pytest-xdist does not fight over the repo default."""
    cache_dir = tmp_path_factory.mktemp("runtime_generated_routes_cache")
    cache_file = cache_dir / "runtime_generated_routes_cache.json"
    mp = pytest.MonkeyPatch()
    mp.setattr(
        "proxbox_api.routes.proxmox.runtime_generated.proxmox_generated_route_cache_path",
        lambda: cache_file,
    )
    yield
    mp.undo()


class SchemaDrivenFakeResource:
    def __init__(self, session, path):
        self.session = session
        self.path = path

    def _invoke(self, http_method: str, **kwargs: Any) -> Any:
        self.session.calls.append((http_method, self.path, kwargs))
        return self.session.responses[(http_method, self.path)]

    def get(self, **kwargs: Any) -> Any:
        return self._invoke("GET", **kwargs)

    def post(self, **kwargs: Any) -> Any:
        return self._invoke("POST", **kwargs)

    def put(self, **kwargs: Any) -> Any:
        return self._invoke("PUT", **kwargs)

    def delete(self, **kwargs: Any) -> Any:
        return self._invoke("DELETE", **kwargs)


class SchemaDrivenFakeSession:
    def __init__(self, responses: dict[tuple[str, str], Any]):
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, path: str) -> SchemaDrivenFakeResource:
        return SchemaDrivenFakeResource(self, path)


class SchemaDrivenFakeTarget:
    def __init__(self, responses: dict[tuple[str, str], Any]):
        self.session = SchemaDrivenFakeSession(responses)


def _single_operation_document(
    *,
    version_tag: str,
    full_document: dict[str, Any],
    openapi_path: str,
    method: str,
    operation: dict[str, Any],
) -> dict[str, Any]:
    return {
        "openapi": full_document.get("openapi", "3.1.0"),
        "info": full_document.get("info", {"title": "Generated Proxmox", "version": version_tag}),
        "paths": {
            openapi_path: {
                method.lower(): operation,
            }
        },
    }


def _generated_model_module(cache_key: str, document: dict[str, Any]) -> ModuleType:
    module = GENERATED_MODEL_MODULES.get(cache_key)
    if module is not None:
        return module

    code = generate_pydantic_models_from_openapi(document)
    module = ModuleType(f"tests.generated_proxmox_models_{cache_key.replace('.', '_')}")
    sys.modules[module.__name__] = module
    exec(code, module.__dict__)
    GENERATED_MODEL_MODULES[cache_key] = module
    return module


def _operation_id(method: str, openapi_path: str, operation: dict[str, Any]) -> str:
    return operation.get("operationId") or f"{method.lower()}_{openapi_path}"


def _expected_forwarded_payload(
    *,
    case: dict[str, Any],
    operation: dict[str, Any],
    model_module: ModuleType,
) -> dict[str, Any]:
    expected_forwarded = {
        original_name: value
        for original_name, value in case["expected_forwarded"].items()
        if original_name not in (case["request_body"] or {})
    }
    request_body = case["request_body"]
    if request_body is None:
        return expected_forwarded

    request_model = getattr(
        model_module,
        f"{pascal_case(_operation_id(case['method'], case['openapi_path'], operation))}Request",
        None,
    )
    if request_model is None:
        expected_forwarded.update(request_body)
        return expected_forwarded

    validated_request = request_model.model_validate(request_body)
    expected_forwarded.update(validated_request.model_dump(by_alias=True, exclude_none=True))
    return expected_forwarded


def _expected_response_json(
    *,
    version_tag: str,
    case: dict[str, Any],
    operation: dict[str, Any],
    response_schema: dict[str, Any] | None,
    model_module: ModuleType,
) -> Any:
    raw_response = _sample_value_for_schema(
        response_schema,
        seed=f"{case['method'].lower()}_{case['openapi_path'].strip('/').replace('/', '_')}_response",
    )
    response_model = getattr(
        model_module,
        f"{pascal_case(_operation_id(case['method'], case['openapi_path'], operation))}Response",
        None,
    )
    if response_model is None:
        return raw_response
    return response_model.model_validate(raw_response).model_dump(mode="json", by_alias=True)


def _assert_forwarded_payload(
    actual_payload: dict[str, Any],
    expected_payload: dict[str, Any],
) -> None:
    assert set(actual_payload) == set(expected_payload)
    for key, expected_value in expected_payload.items():
        if isinstance(expected_value, (str, int, float, bool)) or expected_value is None:
            assert actual_payload[key] == expected_value


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


def _override_async_db_session(db_engine):
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    async_engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    session_factory = async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_async_session():
        async with session_factory() as session:
            yield session

    return async_engine, override_get_async_session


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

    async_engine, override_get_async_session = _override_async_db_session(db_engine)
    app.dependency_overrides[get_session] = _override_db_session(db_engine)
    app.dependency_overrides[get_async_session] = override_get_async_session
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

    asyncio.run(async_engine.dispose())

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

    async_engine, override_get_async_session = _override_async_db_session(db_engine)
    app.dependency_overrides[get_session] = _override_db_session(db_engine)
    app.dependency_overrides[get_async_session] = override_get_async_session
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

    asyncio.run(async_engine.dispose())

    assert missing_selector.status_code == 400
    assert "Multiple Proxmox endpoints configured" in missing_selector.json()["message"]
    assert selected.status_code == 200
    assert selected.json()["params"]["type"] == "vm"


@pytest.mark.parametrize("case", GENERATED_ROUTE_CASES)
def test_every_generated_proxy_route_has_mock_based_schema_validated_coverage(
    monkeypatch,
    tmp_path,
    case,
):
    version_tag = case["version_tag"]
    full_document = load_proxmox_generated_openapi(version_tag=version_tag)
    operation = full_document["paths"][case["openapi_path"]][case["method"].lower()]
    operation_document = _single_operation_document(
        version_tag=version_tag,
        full_document=full_document,
        openapi_path=case["openapi_path"],
        method=case["method"],
        operation=operation,
    )
    model_module = _generated_model_module(
        f"{version_tag}__{case['method']}__{case['openapi_path']}",
        operation_document,
    )
    response_schema = _response_schema(operation)
    expected_response = _expected_response_json(
        version_tag=version_tag,
        case=case,
        operation=operation,
        response_schema=response_schema,
        model_module=model_module,
    )
    expected_forwarded = _expected_forwarded_payload(
        case=case,
        operation=operation,
        model_module=model_module,
    )
    fake_target = SchemaDrivenFakeTarget(
        responses={(case["method"], case["upstream_path"]): expected_response}
    )

    async def fake_resolve_proxmox_target_session(
        database_session,
        source="database",
        name=None,
        domain=None,
        ip_address=None,
    ):
        return fake_target

    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.runtime_generated.resolve_proxmox_target_session",
        fake_resolve_proxmox_target_session,
    )
    monkeypatch.setattr(
        "proxbox_api.routes.proxmox.runtime_generated.proxmox_generated_route_cache_path",
        lambda: tmp_path / "runtime_generated_routes_cache.json",
    )
    monkeypatch.setattr(
        "proxbox_api.app.factory.register_generated_proxmox_routes",
        lambda _app: None,
    )

    register_generated_proxmox_routes(
        app,
        openapi_documents={version_tag: operation_document},
    )

    request_kwargs: dict[str, Any] = {}
    if case["query_values"]:
        request_kwargs["params"] = case["query_values"]
    if case["method"] != "GET" and case["request_body"] is not None:
        request_kwargs["json"] = case["request_body"]

    with TestClient(app) as client:
        response = client.request(case["method"], case["route_path"], **request_kwargs)

    assert response.status_code == 200, response.text
    actual_method, actual_upstream_path, actual_payload = fake_target.session.calls[-1]
    assert actual_method == case["method"]
    assert actual_upstream_path == case["upstream_path"]
    _assert_forwarded_payload(actual_payload, expected_forwarded)
    if _resolved_schema(response_schema).get("type") == "null":
        assert response.json() is None
    else:
        assert response.json() == expected_response


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

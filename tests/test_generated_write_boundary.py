"""Behavioral boundary for generated proxy writes, independent of the schema catalog."""

import inspect
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from proxbox_api.main import app
from proxbox_api.routes.proxmox import runtime_generated

pytestmark = pytest.mark.xdist_group("generated_proxmox_routes")

# The mounted verbs at the branch point are GET, POST, PUT, and DELETE.
# Keep this denial oracle fixed rather than deriving it from the production policy.
MUTATION_METHODS = ("POST", "PUT", "DELETE")
ROUTE_PREFIXES = ("/proxmox/api2/latest", "/proxmox/api2/8.3.0", "/proxmox/api2")
DENIAL = {
    "message": "Generated Proxmox proxy routes are read-only.",
    "detail": "Use a typed, audited RPC procedure for mutation operations.",
    "python_exception": None,
}


@pytest.fixture
def generated_boundary(monkeypatch, tmp_path):
    document = {
        "openapi": "3.1.0",
        "info": {"title": "Boundary test", "version": "latest"},
        "paths": {
            "/boundary": {
                method.lower(): {
                    "operationId": f"{method.lower()}_boundary",
                    "responses": {"200": {"description": "ok"}},
                }
                for method in ("GET", *MUTATION_METHODS)
            }
        },
    }
    documents = {version: deepcopy(document) for version in ("latest", "8.3.0")}
    monkeypatch.setattr(
        runtime_generated,
        "proxmox_generated_route_cache_path",
        lambda: tmp_path / "routes.json",
    )
    monkeypatch.setattr(
        runtime_generated, "available_proxmox_sdk_versions", lambda: list(documents)
    )
    monkeypatch.setattr(
        runtime_generated,
        "load_proxmox_generated_openapi",
        lambda version_tag="latest": documents[version_tag],
    )
    monkeypatch.setattr(
        "proxbox_api.app.factory.register_generated_proxmox_routes", lambda app: None
    )
    resolver = AsyncMock(side_effect=AssertionError("A denied request resolved its target"))
    monkeypatch.setattr(runtime_generated, "resolve_proxmox_target_session", resolver)
    return documents, resolver


@pytest.mark.parametrize("registration", ("fresh", "memory-cache", "disk-cache", "refresh"))
@pytest.mark.parametrize("prefix", ROUTE_PREFIXES)
@pytest.mark.parametrize("method", MUTATION_METHODS)
def test_generated_mutations_never_resolve_targets(
    generated_boundary, auth_headers, registration, prefix, method
):
    documents, resolver = generated_boundary
    runtime_generated.register_generated_proxmox_routes(app, openapi_documents=documents)
    if registration == "memory-cache":
        runtime_generated.register_generated_proxmox_routes(app)
    if registration == "disk-cache":
        runtime_generated.clear_generated_proxmox_routes(app)
        runtime_generated._MODEL_MODULE_CACHE.clear()
        result = runtime_generated.register_generated_proxmox_routes(app)
        assert result["cache_source"] == "runtime-cache"
    if registration == "refresh":
        runtime_generated.register_generated_proxmox_routes(app, force_rebuild=True)
    with TestClient(app) as client:
        response = client.request(
            method,
            f"{prefix}/boundary",
            headers=auth_headers,
            params={"source": "netbox", "target_name": "must-not-resolve"},
        )
    assert response.status_code == 403
    assert response.json() == DENIAL
    resolver.assert_not_awaited()


@pytest.mark.parametrize("method", ("GET", *MUTATION_METHODS))
def test_generated_boundary_preserves_authentication(generated_boundary, method):
    documents, resolver = generated_boundary
    runtime_generated.register_generated_proxmox_routes(app, openapi_documents=documents)
    with TestClient(app) as client:
        response = client.request(method, "/proxmox/api2/latest/boundary")
    assert response.status_code == 401
    resolver.assert_not_awaited()


@pytest.mark.parametrize("method", ("PATCH", "HEAD", "OPTIONS", "TRACE", "future"))
async def test_generated_endpoint_factory_fails_closed_for_unrecognized_methods(method):
    endpoint = runtime_generated._build_generated_endpoint(
        openapi_path="/boundary",
        method=method,
        operation={},
        request_model=None,
        response_model=None,
    )
    from proxbox_api.exception import ProxboxException

    with pytest.raises(ProxboxException) as caught:
        await endpoint()
    assert caught.value.http_status_code == 403


@pytest.mark.parametrize(
    ("used_names", "expected"),
    ((set(), "op_source"), ({"op_source", "op_source_1"}, "op_source_2")),
)
def test_generated_parameter_names_avoid_all_existing_suffixes(used_names, expected):
    assert runtime_generated._unique_parameter_name("op_source", used_names) == expected


def test_generated_signature_ignores_unsupported_and_malformed_parameters():
    endpoint = runtime_generated._build_generated_endpoint(
        openapi_path="/boundary",
        method="GET",
        operation={
            "parameters": [
                None,
                {"name": 1, "in": "query"},
                {"name": "ignored", "in": "header"},
                {"name": "source", "in": "query", "schema": {"type": "string"}},
            ]
        },
        request_model=None,
        response_model=None,
    )
    parameters = inspect.signature(endpoint).parameters
    assert list(parameters) == [
        "_database_session",
        "source",
        "target_name",
        "target_domain",
        "target_ip_address",
        "op_source",
    ]
    assert parameters["op_source"].default.alias == "op_source"

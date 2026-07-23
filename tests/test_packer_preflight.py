"""Endpoint scoping, no-write behavior, and secret-safe Packer contracts."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.main import app
from proxbox_api.routes.cloud import pipeline_scripts, template_images
from proxbox_api.schemas.cloud_provision import (
    CloudImageSSHExecutionTarget,
    CloudImageTemplateBuildRequest,
    CloudImageTemplateExecutionSummary,
    CloudImageTemplatePreflightRequest,
    CloudImageTemplatePreflightResponse,
    PackerFinding,
    PackerFindingSeverity,
    PackerPreflightCapabilityStatus,
)
from proxbox_api.schemas.proxmox import ProxmoxSessionSchema, ProxmoxTokenSchema
from proxbox_api.services import packer_preflight as preflight_service
from proxbox_api.services.packer_preflight import run_packer_preflight

_CANARY = "PACKER-CANARY-SECRET-4d9f0c"
_PUBLIC_IMAGE = f"https://93.184.216.34/image.qcow2?sig={_CANARY}"


def _execution_target(
    identity_file: str = "/etc/proxbox/ssh_keys/id_ed25519",
) -> CloudImageSSHExecutionTarget:
    return CloudImageSSHExecutionTarget(
        host="93.184.216.34",
        user="root",
        port=22,
        identity_file=identity_file,
        known_host_fingerprint="SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    )


@pytest.fixture
def proxbox_caplog(caplog: pytest.LogCaptureFixture):
    """Capture the non-propagating application logger for secret canaries."""

    proxbox_logger = logging.getLogger("proxbox")
    proxbox_logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        proxbox_logger.removeHandler(caplog.handler)


class _ReadOnlyAPI:
    def __init__(
        self,
        *,
        node_rows: object,
        storage_rows: object,
        vm_rows: object,
        nextid_payload: object = 9010,
        fail_paths: set[str] | None = None,
    ) -> None:
        self.node_rows = node_rows
        self.storage_rows = storage_rows
        self.vm_rows = vm_rows
        self.nextid_payload = nextid_payload
        self.fail_paths = fail_paths or set()
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def __call__(self, path: str) -> _ReadOnlyCall:
        payload = {
            "cluster/status": self.node_rows,
            "cluster/nextid": self.nextid_payload,
            "cluster/resources": self.vm_rows,
        }.get(path)
        return _ReadOnlyCall(self, path, payload)

    def nodes(self, node: str) -> _ReadOnlyNode:
        return _ReadOnlyNode(self, node)


class _ReadOnlyCall:
    def __init__(self, api: _ReadOnlyAPI, path: str, payload: object) -> None:
        self.api = api
        self.path = path
        self.payload = payload

    async def get(self, **kwargs: object) -> object:
        self.api.calls.append(("GET", self.path, kwargs))
        if self.path in self.api.fail_paths:
            raise RuntimeError(f"unsupported {_CANARY}")
        return self.payload

    async def post(self, **_kwargs: object) -> object:
        raise AssertionError("Packer preflight must never invoke a write method")


class _ReadOnlyNode:
    def __init__(self, api: _ReadOnlyAPI, node: str) -> None:
        self.storage = _ReadOnlyCall(api, f"nodes/{node}/storage", api.storage_rows)


def _preflight_request(**overrides: object) -> CloudImageTemplatePreflightRequest:
    payload: dict[str, object] = {
        "endpoint_id": 7,
        "target_node": "pve01",
        "vmid": 9010,
        "image_storage": "local",
        "vm_storage": "local-zfs",
        "snippets_storage": "local",
        "recipe_digest": "a" * 64,
    }
    payload.update(overrides)
    return CloudImageTemplatePreflightRequest(**payload)


def _healthy_api(*, vm_rows: object | None = None) -> _ReadOnlyAPI:
    return _ReadOnlyAPI(
        node_rows=[{"type": "node", "name": "pve01", "online": 1}],
        storage_rows=[
            {
                "storage": "local",
                "active": 1,
                "enabled": 1,
                "content": "iso,vztmpl,backup,snippets,images",
            },
            {
                "storage": "local-zfs",
                "active": 1,
                "enabled": 1,
                "content": "images,rootdir",
            },
        ],
        vm_rows=[] if vm_rows is None else vm_rows,
    )


def _install_route_session(
    monkeypatch: pytest.MonkeyPatch,
    *,
    endpoint_id: int,
    api: _ReadOnlyAPI,
    close_error: Exception | None = None,
) -> dict[str, int]:
    closed = {"count": 0}
    schema = SimpleNamespace(db_endpoint_id=endpoint_id)

    class RouteSession:
        session = api

        async def aclose(self) -> None:
            closed["count"] += 1
            if close_error is not None:
                raise close_error

    async def fake_load(**_kwargs: object) -> list[object]:
        return [schema]

    async def fake_create(
        resolved_schema: object,
        *,
        initialize_metadata: bool = True,
    ) -> object:
        assert isinstance(resolved_schema, ProxmoxSessionSchema)
        assert resolved_schema.db_endpoint_id == endpoint_id
        assert initialize_metadata is False
        return RouteSession()

    monkeypatch.setattr(template_images, "load_proxmox_session_schemas", fake_load)
    monkeypatch.setattr(template_images.ProxmoxSession, "create", staticmethod(fake_create))
    return closed


def test_preflight_service_has_no_mutating_call_surface() -> None:
    source = inspect.getsource(preflight_service)
    tree = ast.parse(source)
    called_attributes = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert {"post", "put", "patch", "delete"}.isdisjoint(called_attributes)
    assert "subprocess" not in source
    assert "_gate" not in source


@pytest.mark.asyncio
async def test_preflight_is_read_only_and_independent_of_allow_writes() -> None:
    api = _healthy_api()

    response = await run_packer_preflight(
        _preflight_request(),
        SimpleNamespace(session=api),
        writes_enabled=False,
    )

    assert response.ready is True
    assert response.writes_enabled is False
    assert all(
        item.status == PackerPreflightCapabilityStatus.passed for item in response.capabilities
    )
    assert api.calls == [
        ("GET", "cluster/status", {}),
        ("GET", "nodes/pve01/storage", {}),
        ("GET", "cluster/nextid", {"vmid": 9010}),
        ("GET", "cluster/resources", {"type": "vm"}),
    ]


def test_preflight_production_asgi_requires_authentication(test_client) -> None:
    response = test_client.post(
        "/cloud/templates/images/preflight",
        json=_preflight_request().model_dump(mode="json"),
    )

    assert response.status_code == 401


def test_preflight_production_asgi_success_closes_exact_session(
    auth_test_client,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = ProxmoxEndpoint(
        name="asgi-preflight-success",
        ip_address="93.184.216.40",
        username="root@pam",
        enabled=True,
        allow_writes=False,
    )
    db_session.add(endpoint)
    db_session.commit()
    db_session.refresh(endpoint)
    endpoint_id = int(endpoint.id)
    closed = _install_route_session(
        monkeypatch,
        endpoint_id=endpoint_id,
        api=_healthy_api(),
    )

    response = auth_test_client.post(
        "/cloud/templates/images/preflight",
        json=_preflight_request(endpoint_id=endpoint_id).model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert closed == {"count": 1}


def test_preflight_production_asgi_rejects_disabled_endpoint(
    auth_test_client,
    db_session,
) -> None:
    endpoint = ProxmoxEndpoint(
        name="asgi-preflight-disabled",
        ip_address="93.184.216.41",
        username="root@pam",
        enabled=False,
    )
    db_session.add(endpoint)
    db_session.commit()
    db_session.refresh(endpoint)

    response = auth_test_client.post(
        "/cloud/templates/images/preflight",
        json=_preflight_request(endpoint_id=int(endpoint.id)).model_dump(mode="json"),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "endpoint_disabled"


def test_preflight_production_asgi_malformed_upstream_and_cleanup_are_bounded(
    auth_test_client,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    proxbox_caplog: pytest.LogCaptureFixture,
) -> None:
    endpoint = ProxmoxEndpoint(
        name="asgi-preflight-malformed",
        ip_address="93.184.216.42",
        username="root@pam",
        enabled=True,
    )
    db_session.add(endpoint)
    db_session.commit()
    db_session.refresh(endpoint)
    endpoint_id = int(endpoint.id)
    cleanup_canary = f"cleanup-{_CANARY}"
    api = _healthy_api()
    api.nextid_payload = {"unexpected": "shape"}
    closed = _install_route_session(
        monkeypatch,
        endpoint_id=endpoint_id,
        api=api,
        close_error=RuntimeError(cleanup_canary),
    )

    response = auth_test_client.post(
        "/cloud/templates/images/preflight",
        json=_preflight_request(endpoint_id=endpoint_id).model_dump(mode="json"),
    )

    assert response.status_code == 200
    assert response.json()["ready"] is False
    assert "vmid_payload_invalid" in {finding["code"] for finding in response.json()["findings"]}
    assert closed == {"count": 1}
    assert cleanup_canary not in response.text
    assert cleanup_canary not in proxbox_caplog.text
    assert "error_type=RuntimeError" in proxbox_caplog.text


@pytest.mark.asyncio
async def test_release_preflight_uses_private_staging_without_image_storage_claim() -> None:
    api = _healthy_api()

    response = await run_packer_preflight(
        _preflight_request(image_storage="not-a-pve-storage"),
        SimpleNamespace(session=api),
        writes_enabled=False,
    )

    assert response.ready is True
    assert not any(item.target == "storage:not-a-pve-storage" for item in response.capabilities)


@pytest.mark.parametrize(
    ("provider", "snippets_required"),
    [("release_image", False), ("proxmox_iso", True)],
)
def test_preflight_rejects_snippet_assertion_that_disagrees_with_provider(
    provider: str,
    snippets_required: bool,
) -> None:
    with pytest.raises(ValidationError, match="provider-derived requirement"):
        _preflight_request(provider=provider, snippets_required=snippets_required)


def test_cloud_provision_schema_imports_in_fresh_interpreter_without_route_cycle() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import proxbox_api.schemas.cloud_provision; "
                "import proxbox_api.routes.proxmox.endpoints"
            ),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_preflight_reports_storage_and_vmid_failures_as_typed_findings() -> None:
    api = _healthy_api(vm_rows=[{"type": "qemu", "vmid": 9010, "node": "pve01"}])
    api.fail_paths.add("cluster/nextid")
    api.storage_rows[0]["content"] = "images"

    response = await run_packer_preflight(
        _preflight_request(),
        SimpleNamespace(session=api),
        writes_enabled=True,
    )

    assert response.ready is False
    assert {finding.code for finding in response.findings} >= {
        "storage_content_missing",
        "vmid_in_use",
    }
    assert all(
        set(finding.model_dump()) == {"code", "severity", "target", "message"}
        for finding in response.findings
    )


@pytest.mark.asyncio
async def test_authoritative_nextid_denial_never_passes_hidden_vmid() -> None:
    api = _healthy_api(vm_rows=[])
    api.fail_paths.add("cluster/nextid")

    response = await run_packer_preflight(
        _preflight_request(),
        SimpleNamespace(session=api),
        writes_enabled=False,
    )

    vmid_capability = next(
        item for item in response.capabilities if item.capability.value == "vmid_available"
    )
    assert vmid_capability.status == PackerPreflightCapabilityStatus.unsupported
    assert "vmid_check_unsupported" in {finding.code for finding in response.findings}
    assert response.ready is False


@pytest.mark.asyncio
async def test_iso_provider_requires_iso_content_and_skips_snippets() -> None:
    api = _healthy_api()
    api.storage_rows = [
        {
            "storage": "iso-store",
            "active": 1,
            "enabled": 1,
            "content": "iso",
        },
        {
            "storage": "vm-store",
            "active": 1,
            "enabled": 1,
            "content": "images",
        },
    ]

    response = await run_packer_preflight(
        _preflight_request(
            provider="proxmox_iso",
            image_storage="iso-store",
            vm_storage="vm-store",
            snippets_storage=None,
            snippets_required=False,
        ),
        SimpleNamespace(session=api),
        writes_enabled=False,
    )

    assert response.ready is True
    assert {item.capability.value for item in response.capabilities} == {
        "endpoint_session",
        "node_online",
        "image_storage_iso",
        "vm_storage_images",
        "vmid_available",
    }
    assert not any(item.target.startswith("storage:local") for item in response.capabilities)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("node_rows", "expected_code"),
    [
        ([], "node_not_found"),
        ([{"type": "node", "name": "pve01", "status": "offline"}], "node_offline"),
    ],
)
async def test_preflight_reports_typed_node_failures(
    node_rows: object,
    expected_code: str,
) -> None:
    api = _healthy_api()
    api.node_rows = node_rows

    response = await run_packer_preflight(
        _preflight_request(),
        SimpleNamespace(session=api),
        writes_enabled=False,
    )

    assert response.ready is False
    assert expected_code in {finding.code for finding in response.findings}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload_attribute", "payload", "expected_code"),
    [
        ("node_rows", None, "node_payload_invalid"),
        ("node_rows", {"unexpected": "shape"}, "node_payload_invalid"),
        ("storage_rows", {"data": {}}, "storage_payload_invalid"),
        ("nextid_payload", {"unexpected": "shape"}, "vmid_payload_invalid"),
        ("nextid_payload", True, "vmid_payload_invalid"),
        ("nextid_payload", "not-a-number", "vmid_payload_invalid"),
    ],
)
async def test_preflight_malformed_collections_fail_closed(
    payload_attribute: str,
    payload: object,
    expected_code: str,
) -> None:
    api = _healthy_api()
    setattr(api, payload_attribute, payload)

    response = await run_packer_preflight(
        _preflight_request(),
        SimpleNamespace(session=api),
        writes_enabled=False,
    )

    assert response.ready is False
    assert expected_code in {finding.code for finding in response.findings}
    targets = {finding.target for finding in response.findings if finding.code == expected_code}
    assert targets
    assert all(
        capability.status == PackerPreflightCapabilityStatus.unsupported
        for capability in response.capabilities
        if capability.target in targets
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("storage_row", "expected_code", "expected_status"),
    [
        (None, "storage_not_found", PackerPreflightCapabilityStatus.failed),
        (
            {"storage": "local", "enabled": 0, "active": 1, "content": "images,snippets"},
            "storage_disabled",
            PackerPreflightCapabilityStatus.failed,
        ),
        (
            {"storage": "local", "enabled": 1, "active": 0, "content": "images,snippets"},
            "storage_inactive",
            PackerPreflightCapabilityStatus.failed,
        ),
        (
            {"storage": "local", "enabled": 1, "active": 1},
            "storage_content_check_unsupported",
            PackerPreflightCapabilityStatus.unsupported,
        ),
        (
            {"storage": "local", "enabled": 1, "content": "images,snippets"},
            "storage_state_check_unsupported",
            PackerPreflightCapabilityStatus.unsupported,
        ),
        (
            {"storage": "local", "active": 1, "content": "images,snippets"},
            "storage_state_check_unsupported",
            PackerPreflightCapabilityStatus.unsupported,
        ),
    ],
)
async def test_preflight_reports_typed_storage_failures(
    storage_row: dict[str, object] | None,
    expected_code: str,
    expected_status: PackerPreflightCapabilityStatus,
) -> None:
    api = _healthy_api()
    api.storage_rows = [row for row in api.storage_rows if row["storage"] != "local"]
    if storage_row is not None:
        api.storage_rows.append(storage_row)

    response = await run_packer_preflight(
        _preflight_request(),
        SimpleNamespace(session=api),
        writes_enabled=False,
    )

    assert response.ready is False
    assert expected_code in {finding.code for finding in response.findings}
    assert expected_status in {
        item.status for item in response.capabilities if item.target == "storage:local"
    }


@pytest.mark.asyncio
async def test_preflight_unsupported_checks_are_typed_and_secret_free() -> None:
    api = _healthy_api()
    api.fail_paths = {
        "cluster/status",
        "nodes/pve01/storage",
        "cluster/nextid",
        "cluster/resources",
    }

    response = await run_packer_preflight(
        _preflight_request(),
        SimpleNamespace(session=api),
        writes_enabled=False,
    )
    serialized = response.model_dump_json()

    assert response.ready is False
    assert {finding.code for finding in response.findings} >= {
        "node_check_unsupported",
        "storage_check_unsupported",
        "vmid_check_unsupported",
    }
    assert _CANARY not in serialized


@pytest.mark.asyncio
async def test_preflight_target_resolver_selects_only_exact_enabled_endpoint(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = ProxmoxEndpoint(
        name="selected",
        ip_address="93.184.216.34",
        username="root@pam",
        enabled=True,
        allow_writes=False,
    )
    db_session.add(endpoint)
    db_session.commit()
    db_session.refresh(endpoint)
    endpoint_id = int(endpoint.id)
    exact_schema = SimpleNamespace(db_endpoint_id=endpoint_id)
    other_schema = SimpleNamespace(db_endpoint_id=endpoint_id + 1)
    created = SimpleNamespace(aclose=lambda: None)

    async def fake_load(**kwargs: object) -> list[object]:
        assert kwargs["source"] == "database"
        assert kwargs["endpoint_ids"] == [endpoint_id]
        return [other_schema, exact_schema]

    async def fake_create(schema: object, *, initialize_metadata: bool = True) -> object:
        assert isinstance(schema, ProxmoxSessionSchema)
        assert schema.db_endpoint_id == endpoint_id
        assert schema.ip_address == endpoint.ip_address
        assert initialize_metadata is False
        return created

    monkeypatch.setattr(template_images, "load_proxmox_session_schemas", fake_load)
    monkeypatch.setattr(template_images.ProxmoxSession, "create", staticmethod(fake_create))

    resolved_endpoint, resolved_session = await template_images._resolve_preflight_target(
        db_session,
        endpoint_id,
    )

    assert resolved_endpoint.id == endpoint_id
    assert resolved_session is created


@pytest.mark.asyncio
async def test_preflight_target_resolver_rejects_ambiguous_matching_sessions(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = ProxmoxEndpoint(
        name="ambiguous",
        ip_address="93.184.216.35",
        username="root@pam",
        enabled=True,
    )
    db_session.add(endpoint)
    db_session.commit()
    db_session.refresh(endpoint)
    endpoint_id = int(endpoint.id)

    async def fake_load(**_kwargs: object) -> list[object]:
        return [
            SimpleNamespace(db_endpoint_id=endpoint_id),
            SimpleNamespace(db_endpoint_id=endpoint_id),
        ]

    monkeypatch.setattr(template_images, "load_proxmox_session_schemas", fake_load)

    with pytest.raises(HTTPException) as exc:
        await template_images._resolve_preflight_target(db_session, endpoint_id)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "endpoint_session_ambiguous"


@pytest.mark.asyncio
async def test_preflight_target_resolver_rejects_missing_exact_session(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = ProxmoxEndpoint(
        name="missing-session",
        ip_address="93.184.216.37",
        username="root@pam",
        enabled=True,
    )
    db_session.add(endpoint)
    db_session.commit()
    db_session.refresh(endpoint)
    endpoint_id = int(endpoint.id)

    async def fake_load(**_kwargs: object) -> list[object]:
        return [SimpleNamespace(db_endpoint_id=endpoint_id + 1)]

    monkeypatch.setattr(template_images, "load_proxmox_session_schemas", fake_load)

    with pytest.raises(HTTPException) as exc:
        await template_images._resolve_preflight_target(db_session, endpoint_id)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "endpoint_session_missing"


@pytest.mark.asyncio
async def test_preflight_target_resolver_scrubs_session_factory_failure(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    proxbox_caplog: pytest.LogCaptureFixture,
) -> None:
    endpoint = ProxmoxEndpoint(
        name="unavailable",
        ip_address="93.184.216.36",
        username="root@pam",
        enabled=True,
    )
    db_session.add(endpoint)
    db_session.commit()
    db_session.refresh(endpoint)
    endpoint_id = int(endpoint.id)
    session_canary = "https://root:password@pve.example/?token=PACKER-SESSION-SECRET"
    close_canary = "https://root:password@pve.example/?token=PACKER-CLOSE-SECRET"
    schema = ProxmoxSessionSchema(
        ip_address="93.184.216.36",
        domain="pve.example",
        http_port=8006,
        user="root@pam",
        password="password",
        token=ProxmoxTokenSchema(),
        ssl=False,
        db_endpoint_id=endpoint_id,
    )

    class SecretFailingSDK:
        def __init__(self, _host: str, **_kwargs: object) -> None:
            self.version = self

        def get(self) -> object:
            raise RuntimeError(session_canary)

        async def close(self) -> None:
            raise RuntimeError(close_canary)

    async def fake_load(**_kwargs: object) -> list[object]:
        return [schema]

    monkeypatch.setattr(template_images, "load_proxmox_session_schemas", fake_load)
    monkeypatch.setattr("proxbox_api.session.proxmox.ProxmoxAPI", SecretFailingSDK)
    proxbox_caplog.set_level(logging.DEBUG)

    with pytest.raises(HTTPException) as exc:
        await template_images._resolve_preflight_target(db_session, endpoint_id)

    assert exc.value.status_code == 503
    assert exc.value.detail == {
        "code": "endpoint_session_unavailable",
        "endpoint_id": endpoint_id,
        "message": "The selected Proxmox endpoint session is unavailable.",
    }
    serialized = json.dumps(exc.value.detail)
    assert session_canary not in serialized
    assert close_canary not in serialized
    assert session_canary not in proxbox_caplog.text
    assert close_canary not in proxbox_caplog.text


@pytest.mark.asyncio
async def test_preflight_target_resolver_scrubs_session_loader_failure(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    endpoint = ProxmoxEndpoint(
        name="loader-unavailable",
        ip_address="93.184.216.38",
        username="root@pam",
        enabled=True,
    )
    db_session.add(endpoint)
    db_session.commit()
    db_session.refresh(endpoint)
    endpoint_id = int(endpoint.id)
    canary = "https://root:password@pve.example/?token=PACKER-LOADER-SECRET"

    async def fake_load(**_kwargs: object) -> list[object]:
        raise RuntimeError(canary)

    monkeypatch.setattr(template_images, "load_proxmox_session_schemas", fake_load)

    with pytest.raises(HTTPException) as exc:
        await template_images._resolve_preflight_target(db_session, endpoint_id)

    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "endpoint_session_unavailable"
    assert canary not in json.dumps(exc.value.detail)
    assert canary not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "endpoint_offset", "expected_status", "expected_code"),
    [
        (True, 1000, 404, "endpoint_not_found"),
        (False, 0, 422, "endpoint_disabled"),
    ],
)
async def test_preflight_target_resolver_rejects_missing_or_disabled_endpoint(
    db_session,
    enabled: bool,
    endpoint_offset: int,
    expected_status: int,
    expected_code: str,
) -> None:
    endpoint = ProxmoxEndpoint(
        name=f"endpoint-{enabled}",
        ip_address="93.184.216.36",
        username="root@pam",
        enabled=enabled,
    )
    db_session.add(endpoint)
    db_session.commit()
    db_session.refresh(endpoint)

    with pytest.raises(HTTPException) as exc:
        await template_images._resolve_preflight_target(
            db_session,
            int(endpoint.id) + endpoint_offset,
        )

    assert exc.value.status_code == expected_status
    assert exc.value.detail["code"] == expected_code


@pytest.mark.asyncio
async def test_preflight_route_suppresses_secret_bearing_cleanup_failure(
    db_session,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    canary = "https://root:password@pve.example/?token=PACKER-CLOSE-SECRET"

    class FailingCloseSession:
        async def aclose(self) -> None:
            raise RuntimeError(canary)

    expected = CloudImageTemplatePreflightResponse(
        endpoint_id=7,
        target_node="pve01",
        vmid=9010,
        ready=False,
        writes_enabled=False,
        recipe_digest="a" * 64,
    )

    async def fake_resolve(_session: object, _endpoint_id: int) -> tuple[object, object]:
        return SimpleNamespace(allow_writes=False), FailingCloseSession()

    async def fake_preflight(*_args: object, **_kwargs: object) -> object:
        return expected

    monkeypatch.setattr(template_images, "_resolve_preflight_target", fake_resolve)
    monkeypatch.setattr(template_images, "run_packer_preflight", fake_preflight)

    response = await template_images.preflight_cloud_image_template(
        _preflight_request(),
        db_session,
    )

    assert response is expected
    assert canary not in response.model_dump_json()
    assert canary not in caplog.text


@pytest.mark.parametrize("cancellation_count", [2, 3])
@pytest.mark.asyncio
async def test_session_close_completes_through_repeated_cancellation(
    cancellation_count: int,
) -> None:
    close_entered = asyncio.Event()
    release_close = asyncio.Event()
    close_completed = False

    class BlockingCloseSession:
        async def aclose(self) -> None:
            nonlocal close_completed
            close_entered.set()
            await release_close.wait()
            close_completed = True

    task = asyncio.create_task(
        template_images._close_proxmox_session(
            BlockingCloseSession(),  # type: ignore[arg-type]
            context="repeated-cancel test",
        )
    )
    await close_entered.wait()
    for _ in range(cancellation_count):
        task.cancel()
        await asyncio.sleep(0)
    assert task.done() is False
    release_close.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert close_completed is True


def _secret_build_request(**overrides: object) -> CloudImageTemplateBuildRequest:
    payload: dict[str, object] = {
        "endpoint_id": 7,
        "target_node": "pve01",
        "vmid": 9010,
        "name": "secret-safe-template",
        "image_url": _PUBLIC_IMAGE,
        "user_data_yaml": f"#cloud-config\npassword: {_CANARY}\n",
        "ssh_authorized_keys": [f"ssh-ed25519 {_CANARY}"],
    }
    payload.update(overrides)
    return CloudImageTemplateBuildRequest(**payload)


def test_default_build_response_omits_all_sensitive_source_surfaces() -> None:
    response = pipeline_scripts.build_pipeline_response(_secret_build_request())
    serialized = response.model_dump_json(exclude_none=True)
    payload = json.loads(serialized)

    assert response.contract_version == "2.0"
    assert response.sensitive_preview is None
    assert _CANARY not in serialized
    assert {
        "generated_userdata",
        "first_boot_script",
        "build_script",
        "commands",
        "image_url",
        "stdout",
        "stderr",
        "source_tree_path",
        "source_artifact_path",
    }.isdisjoint(payload)


def test_sensitive_preview_requires_explicit_non_execution() -> None:
    with pytest.raises(ValidationError) as exc:
        _secret_build_request(include_sensitive_preview=True)
    assert "requires execute=false explicitly" in str(exc.value)

    with pytest.raises(ValidationError) as exc:
        _secret_build_request(execute=True, include_sensitive_preview=True)
    assert "sensitive previews are unavailable to executable requests" in str(exc.value)

    response = pipeline_scripts.build_pipeline_response(
        _secret_build_request(execute=False, include_sensitive_preview=True)
    )
    assert response.sensitive_preview is not None
    assert _CANARY in response.sensitive_preview.build_script
    assert "Sensitive preview" in response.sensitive_preview.warning


def test_legacy_storage_alias_normalizes_without_two_storage_authorities() -> None:
    request = _secret_build_request(storage="legacy-vm-store")

    assert request.vm_storage == "legacy-vm-store"
    assert "vm_storage" in request.model_fields_set
    assert "storage" not in request.model_dump()
    response = pipeline_scripts.build_pipeline_response(
        request.model_copy(update={"execute": False, "include_sensitive_preview": True})
    )
    assert response.sensitive_preview is not None
    assert " legacy-vm-store\n" in response.sensitive_preview.build_script

    with pytest.raises(ValidationError, match="storage and vm_storage must match"):
        _secret_build_request(storage="legacy", vm_storage="canonical")


@pytest.mark.asyncio
async def test_executable_build_response_summarizes_output_without_returning_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION", "true")

    async def fake_execution(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        return (
            "failed",
            7,
            CloudImageTemplateExecutionSummary(
                attempted=True,
                enabled=True,
                exit_code=7,
                stdout_bytes=len(_CANARY),
                stderr_bytes=len(_CANARY),
                stdout_lines=1,
                stderr_lines=1,
            ),
            [
                PackerFinding(
                    code="execution_failed",
                    severity=PackerFindingSeverity.error,
                    target="endpoint:7",
                    message="Remote execution failed; inspect protected host logs.",
                )
            ],
            "execution_failed",
        )

    monkeypatch.setattr(pipeline_scripts, "_pipeline_execution_result", fake_execution)
    response, _error_code = await pipeline_scripts.execute_pipeline_response(
        _secret_build_request(execute=True, ssh_host="93.184.216.34"),
        execution_target=_execution_target(),
        operation_id="00000000-0000-0000-0000-000000000001",
        remote_unit="proxbox-cloud-image-00000000-0000-0000-0000-000000000001",
    )
    serialized = response.model_dump_json(exclude_none=True)

    assert response.status == "failed"
    assert response.execution.attempted is True
    assert response.execution.exit_code == 7
    assert response.execution.stdout_lines == 1
    assert response.execution.stderr_lines == 1
    assert response.sensitive_preview is None
    assert _CANARY not in serialized
    assert "stdout" not in json.loads(serialized)
    assert "stderr" not in json.loads(serialized)


@pytest.mark.parametrize("error_type", [OSError, RuntimeError])
@pytest.mark.asyncio
async def test_subprocess_start_error_is_stable_bounded_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
    proxbox_caplog: pytest.LogCaptureFixture,
    error_type: type[Exception],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION", "true")
    key_dir = tmp_path / "ssh_keys"
    key_dir.mkdir()
    identity_file = key_dir / "id_ed25519"
    identity_file.write_text("test-private-key", encoding="utf-8")
    identity_file.chmod(0o600)
    monkeypatch.setenv("PROXBOX_SSH_KEY_DIR", str(key_dir))

    async def fail(*_args: object, **_kwargs: object) -> None:
        raise error_type(f"ssh unavailable {_CANARY}")

    async def fake_pin(_target: object) -> Path:
        return tmp_path / "known_hosts"

    monkeypatch.setattr(pipeline_scripts.asyncio, "create_subprocess_exec", fail)
    monkeypatch.setattr(pipeline_scripts, "_pinned_known_hosts_file", fake_pin)
    response, _error_code = await pipeline_scripts.execute_pipeline_response(
        _secret_build_request(execute=True, ssh_host="93.184.216.34"),
        execution_target=_execution_target(str(identity_file)),
        operation_id="00000000-0000-0000-0000-000000000001",
        remote_unit="proxbox-cloud-image-00000000-0000-0000-0000-000000000001",
    )
    serialized = response.model_dump_json(exclude_none=True)

    assert response.status == "failed"
    assert response.diagnostics[0].code == "execution_unavailable"
    assert _CANARY not in serialized
    assert _CANARY not in proxbox_caplog.text
    assert len(response.diagnostics[0].message) <= 512


@pytest.mark.asyncio
async def test_subprocess_timeout_output_is_never_returned_or_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("PROXBOX_ENABLE_CLOUD_IMAGE_EXECUTION", "true")

    async def timeout(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        return (
            "failed",
            None,
            CloudImageTemplateExecutionSummary(
                attempted=True,
                enabled=True,
                cancellation_attempted=True,
                cancellation_succeeded=True,
            ),
            [
                PackerFinding(
                    code="execution_timeout",
                    severity=PackerFindingSeverity.error,
                    target="endpoint:7",
                    message="Remote execution exceeded its time limit.",
                )
            ],
            "execution_timeout",
        )

    monkeypatch.setattr(pipeline_scripts, "_pipeline_execution_result", timeout)
    response, _error_code = await pipeline_scripts.execute_pipeline_response(
        _secret_build_request(execute=True, ssh_host="93.184.216.34"),
        execution_target=_execution_target(),
        operation_id="00000000-0000-0000-0000-000000000001",
        remote_unit="proxbox-cloud-image-00000000-0000-0000-0000-000000000001",
    )
    serialized = response.model_dump_json(exclude_none=True)

    assert response.status == "failed"
    assert response.diagnostics[0].code == "execution_timeout"
    assert _CANARY not in serialized
    assert _CANARY not in caplog.text


def test_openapi_pins_versioned_preflight_and_safe_build_response() -> None:
    app.openapi_schema = None
    schema = app.openapi()
    operation = schema["paths"]["/cloud/templates/images/preflight"]["post"]
    request_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert request_ref.endswith("/CloudImageTemplatePreflightRequest")
    response_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    assert response_ref.endswith("/CloudImageTemplatePreflightResponse")

    components = schema["components"]["schemas"]
    preflight_request = components["CloudImageTemplatePreflightRequest"]
    assert preflight_request["properties"]["contract_version"]["const"] == "1.0"
    assert preflight_request["properties"]["provider"]["$ref"].endswith("/CloudImageBuildProvider")
    preflight = components["CloudImageTemplatePreflightResponse"]
    assert preflight["properties"]["contract_version"]["const"] == "1.0"
    assert {
        "recipe_digest",
        "plan_id",
        "plan_digest",
        "plan_token",
        "expires_at",
    }.issubset(preflight["properties"])
    finding = components["PackerFinding"]
    assert set(finding["required"]) == {"code", "severity", "target", "message"}

    operation_path = "/cloud/templates/images/operations/{operation_id}"
    assert operation_path in schema["paths"]
    assert "get" in schema["paths"][operation_path]
    assert "post" in schema["paths"][f"{operation_path}/cancel"]
    operation_response = components["CloudImageBuildOperationResponse"]
    assert {
        "operation_id",
        "state",
        "recipe_digest",
        "plan_digest",
        "verified",
        "recovery_required",
        "cancel_requested",
    }.issubset(operation_response["properties"])

    build_properties = components["CloudImageTemplateBuildResponse"]["properties"]
    assert build_properties["contract_version"]["const"] == "2.0"
    assert {
        "generated_userdata",
        "first_boot_script",
        "build_script",
        "commands",
        "image_url",
        "stdout",
        "stderr",
    }.isdisjoint(build_properties)
    assert "sensitive_preview" in build_properties
    build_request_properties = components["CloudImageTemplateBuildRequest"]["properties"]
    assert "storage" not in build_request_properties
    assert "vm_storage" in build_request_properties
    source_build_command = components["CloudImageSourceBuildCommand"]
    assert set(source_build_command["enum"]) == {"pfsense_memstickserial", "opnsense_dvd"}


def test_producer_preflight_artifact_fixture_validates() -> None:
    fixture = Path(__file__).parent / "fixtures" / "packer_preflight_v1.json"
    response = CloudImageTemplatePreflightResponse.model_validate_json(fixture.read_text())

    assert response.contract_version == "1.0"
    assert response.endpoint_id == 7
    assert response.ready is True
    assert len(response.findings) == len(response.capabilities)

"""Signed-plan binding, durable lease, and verification state-machine tests."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.database import CloudImageBuildOperation, ProxmoxEndpoint
from proxbox_api.routes.cloud import pipeline_scripts, template_images
from proxbox_api.schemas.cloud_provision import (
    CloudImageBuildProvider,
    CloudImageBuildTarget,
    CloudImageTemplateBuildRequest,
    CloudImageTemplateExecutionSummary,
    CloudImageTemplatePreflightResponse,
)
from proxbox_api.services.packer_plans import (
    PackerPlanError,
    acquire_operation_lease,
    finish_operation,
    issue_packer_plan,
    verify_packer_plan,
)
from proxbox_api.session.proxmox import ProxmoxSession


class _ConsumerPreflightFinding(BaseModel):
    """Independent consumer-shaped view; deliberately does not import producer types."""

    model_config = ConfigDict(extra="forbid")

    code: str
    severity: Literal["info", "warning", "error"]
    target: str
    message: str


class _ConsumerPreflightCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: Literal[
        "endpoint_session",
        "node_online",
        "image_storage_images",
        "image_storage_iso",
        "vm_storage_images",
        "snippets_storage_snippets",
        "vmid_available",
    ]
    status: Literal["passed", "failed", "unsupported"]
    target: str


class _ConsumerPreflightResponse(BaseModel):
    """Producer-owned approximation of the pending netbox-packer v1 parser."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1.0"]
    endpoint_id: int
    target_node: str
    vmid: int
    ready: bool
    writes_enabled: bool
    recipe_digest: str | None = None
    plan_id: str | None = None
    plan_digest: str | None = None
    plan_token: str | None = None
    expires_at: float | None = None
    capabilities: list[_ConsumerPreflightCapability]
    findings: list[_ConsumerPreflightFinding]


def _endpoint(**overrides: object) -> ProxmoxEndpoint:
    values: dict[str, object] = {
        "id": 17,
        "name": "pve-plan-bound",
        "ip_address": "93.184.216.34",
        "username": "root@pam",
        "enabled": True,
        "allow_writes": True,
        "access_methods": "api_ssh",
        "ssh_target_node": "pve01",
        "ssh_host": "93.184.216.34",
        "ssh_username": "root",
        "ssh_port": 22,
        "ssh_identity_file": "/etc/proxbox/ssh_keys/id_ed25519",
        "ssh_known_host_fingerprint": ("SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
    }
    values.update(overrides)
    return ProxmoxEndpoint(**values)


def _target(**overrides: object) -> CloudImageBuildTarget:
    values: dict[str, object] = {
        "target_node": "pve01",
        "vmid": 9010,
        "provider": "release_image",
        "image_storage": "local",
        "vm_storage": "local-zfs",
        "snippets_storage": "local",
    }
    values.update(overrides)
    return CloudImageBuildTarget(**values)


@pytest.mark.parametrize(
    "mutation",
    ["token", "endpoint", "target", "recipe", "expired"],
)
def test_signed_plan_rejects_tamper_drift_and_expiry(mutation: str) -> None:
    endpoint = _endpoint()
    target = _target()
    recipe_digest = "a" * 64
    plan, _digest, token = issue_packer_plan(
        endpoint=endpoint,
        target=target,
        recipe_digest=recipe_digest,
        now=1000,
    )

    verify_endpoint = endpoint
    verify_target = target
    verify_recipe = recipe_digest
    verify_token = token
    verify_time = 1001
    expected_code = "preflight_plan_mismatch"
    if mutation == "token":
        verify_token = f"{token[:-1]}{'A' if token[-1] != 'A' else 'B'}"
        expected_code = "preflight_plan_invalid"
    elif mutation == "endpoint":
        verify_endpoint = _endpoint(ssh_host="93.184.216.35")
    elif mutation == "target":
        verify_target = _target(vmid=9011)
    elif mutation == "recipe":
        verify_recipe = "b" * 64
    else:
        verify_time = plan.expires_at
        expected_code = "preflight_plan_expired"

    with pytest.raises(PackerPlanError) as exc:
        verify_packer_plan(
            verify_token,
            endpoint=verify_endpoint,
            target=verify_target,
            recipe_digest=verify_recipe,
            now=verify_time,
        )

    assert exc.value.code == expected_code


@pytest.mark.asyncio
async def test_ready_preflight_returns_secret_free_signed_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "PACKER-ENDPOINT-CREDENTIAL-CANARY"
    endpoint = _endpoint(token_value=canary)
    request = template_images.CloudImageTemplatePreflightRequest(
        endpoint_id=17,
        target_node="pve01",
        vmid=9010,
        provider="release_image",
        image_storage="local",
        vm_storage="local-zfs",
        snippets_storage="local",
        recipe_digest="a" * 64,
    )

    class FakeProxmox:
        async def aclose(self) -> None:
            return None

    async def fake_resolve(*_args: object, **_kwargs: object):
        return endpoint, FakeProxmox()

    async def fake_preflight(*_args: object, **_kwargs: object):
        return CloudImageTemplatePreflightResponse(
            endpoint_id=17,
            target_node="pve01",
            vmid=9010,
            ready=True,
            writes_enabled=True,
            recipe_digest="a" * 64,
        )

    monkeypatch.setattr(template_images, "_resolve_preflight_target", fake_resolve)
    monkeypatch.setattr(template_images, "run_packer_preflight", fake_preflight)

    response = await template_images.preflight_cloud_image_template(request, object())

    assert response.plan_id is not None
    assert response.plan_digest is not None
    assert response.plan_token is not None
    assert response.expires_at is not None
    assert canary not in response.model_dump_json()
    verified, digest = verify_packer_plan(
        response.plan_token,
        endpoint=endpoint,
        target=request.build_target(),
        recipe_digest=request.recipe_digest,
    )
    assert verified.plan_id == response.plan_id
    assert digest == response.plan_digest


@pytest.mark.asyncio
async def test_preflight_session_skips_broad_post_connect_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"post_connect": 0}

    async def fake_auth(_self: ProxmoxSession) -> object:
        return object()

    async def fail_post_connect(_self: ProxmoxSession) -> None:
        calls["post_connect"] += 1

    monkeypatch.setattr(ProxmoxSession, "_auth_async", fake_auth)
    monkeypatch.setattr(ProxmoxSession, "_post_connect_init", fail_post_connect)
    session = await ProxmoxSession.create(
        {
            "ip_address": "93.184.216.34",
            "domain": None,
            "http_port": 8006,
            "user": "root@pam",
            "password": None,
            "token": {"name": "preflight", "value": "secret"},
            "ssl": True,
        },
        initialize_metadata=False,
    )

    assert session.CONNECTED is True
    assert session.mode == "restricted"
    assert calls["post_connect"] == 0


@pytest.mark.asyncio
async def test_operation_journal_rejects_replay_and_concurrent_target_lease(db_engine) -> None:
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    endpoint = _endpoint()
    target = _target()
    now = time.time()
    first, first_digest, _token = issue_packer_plan(
        endpoint=endpoint,
        target=target,
        recipe_digest="a" * 64,
        now=now,
    )
    second, second_digest, _token = issue_packer_plan(
        endpoint=endpoint,
        target=target,
        recipe_digest="a" * 64,
        now=now,
    )

    async with factory() as session:
        operation = await acquire_operation_lease(
            session,
            plan=first,
            plan_digest=first_digest,
            now=now,
        )
        with pytest.raises(PackerPlanError, match="preflight_plan_already_consumed"):
            await acquire_operation_lease(
                session,
                plan=first,
                plan_digest=first_digest,
                now=now,
            )
        with pytest.raises(PackerPlanError, match="build_target_leased"):
            await acquire_operation_lease(
                session,
                plan=second,
                plan_digest=second_digest,
                now=now,
            )

        await finish_operation(
            session,
            operation,
            state="failed",
            execution=CloudImageTemplateExecutionSummary(attempted=True, enabled=True),
            verified=False,
            recovery_required=True,
            error_code="test_failure",
        )
        replacement = await acquire_operation_lease(
            session,
            plan=second,
            plan_digest=second_digest,
            now=now,
        )
        assert replacement.lease_key == "17:9010"
        assert replacement.remote_unit.endswith(second.plan_id)

    await engine.dispose()


@pytest.mark.parametrize(
    ("artifact_verified", "expected_status", "expected_state"),
    [
        (True, "completed", "completed"),
        (False, "recovery_required", "recovery_required"),
    ],
)
@pytest.mark.asyncio
async def test_bound_execution_never_succeeds_without_final_artifact_verification(
    db_engine,
    monkeypatch: pytest.MonkeyPatch,
    artifact_verified: bool,
    expected_status: str,
    expected_state: str,
) -> None:
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    endpoint = _endpoint(id=None)

    async with factory() as session:
        session.add(endpoint)
        await session.commit()
        await session.refresh(endpoint)
        request = CloudImageTemplateBuildRequest(
            endpoint_id=endpoint.id,
            target_node="pve01",
            vmid=9010,
            name="verified-template",
            product_type="pfsense",
            product_version="2.8.1",
            provider="release_image",
            image_storage="local",
            vm_storage="local-zfs",
            snippets_storage="local",
            execute=True,
        )
        target, recipe_digest = pipeline_scripts.pipeline_execution_contract(request)
        _plan, _plan_digest, token = issue_packer_plan(
            endpoint=endpoint,
            target=target,
            recipe_digest=recipe_digest,
        )
        request = request.model_copy(update={"preflight_plan_token": token})

        class FakeProxmox:
            async def aclose(self) -> None:
                return None

        async def fake_resolve(*_args: object, **_kwargs: object):
            return endpoint, FakeProxmox()

        async def fake_preflight(*_args: object, **_kwargs: object):
            return CloudImageTemplatePreflightResponse(
                endpoint_id=int(endpoint.id or 0),
                target_node="pve01",
                vmid=9010,
                ready=True,
                writes_enabled=True,
                recipe_digest=recipe_digest,
            )

        async def fake_execute(
            req: CloudImageTemplateBuildRequest,
            **kwargs: object,
        ):
            planned = pipeline_scripts.build_pipeline_response(
                req.model_copy(update={"execute": False, "preflight_plan_token": None})
            )
            return (
                planned.model_copy(
                    update={
                        "status": "verification_pending",
                        "operation_id": kwargs["operation_id"],
                        "execution": CloudImageTemplateExecutionSummary(
                            attempted=True,
                            enabled=True,
                            exit_code=0,
                        ),
                    }
                ),
                None,
            )

        async def fake_verify(*_args: object, **_kwargs: object):
            return artifact_verified, not artifact_verified

        monkeypatch.setattr(template_images, "_resolve_preflight_target", fake_resolve)
        monkeypatch.setattr(template_images, "run_packer_preflight", fake_preflight)
        monkeypatch.setattr(template_images, "execute_pipeline_response", fake_execute)
        monkeypatch.setattr(template_images, "_verify_pipeline_artifact", fake_verify)

        response = await template_images._execute_bound_pipeline(request, session, endpoint)
        assert response.status == expected_status
        assert response.verified is artifact_verified

        operation = await session.get(CloudImageBuildOperation, response.operation_id)
        assert operation is not None
        assert operation.state == expected_state
        assert operation.verified is artifact_verified
        assert operation.lease_key is None
        assert operation.stdout_bytes == 0
        assert operation.stderr_bytes == 0

        with pytest.raises(template_images.HTTPException) as exc:
            await template_images._execute_bound_pipeline(request, session, endpoint)
        assert exc.value.detail["code"] == "preflight_plan_already_consumed"

    await engine.dispose()


def test_recipe_digest_changes_with_build_affecting_input_and_ignores_execute_flag() -> None:
    base = CloudImageTemplateBuildRequest(
        endpoint_id=17,
        target_node="pve01",
        vmid=9010,
        product_type="pfsense",
        product_version="2.8.1",
        provider=CloudImageBuildProvider.release_image,
        execute=False,
    )

    target, digest = pipeline_scripts.pipeline_execution_contract(base)
    executing_target, executing_digest = pipeline_scripts.pipeline_execution_contract(
        base.model_copy(update={"execute": True})
    )
    _changed_target, changed_digest = pipeline_scripts.pipeline_execution_contract(
        base.model_copy(update={"vmid": 9011})
    )

    assert target == executing_target
    assert digest == executing_digest
    assert digest != changed_digest
    assert len(digest) == 64


def test_producer_owned_netbox_packer_shape_accepts_v1_fixture() -> None:
    """Check local compatibility intent without claiming downstream validation."""

    fixture = Path(__file__).parent / "fixtures" / "netbox_packer_preflight_v1.json"
    payload = fixture.read_text(encoding="utf-8")

    consumer_view = _ConsumerPreflightResponse.model_validate_json(payload)
    producer_view = CloudImageTemplatePreflightResponse.model_validate_json(payload)

    assert consumer_view.contract_version == producer_view.contract_version == "1.0"
    assert consumer_view.recipe_digest == producer_view.recipe_digest
    assert consumer_view.plan_digest == producer_view.plan_digest
    assert consumer_view.plan_token == producer_view.plan_token
    assert len(consumer_view.capabilities) == len(producer_view.capabilities)

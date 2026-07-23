"""Signed-plan binding, durable lease, and verification state-machine tests."""

from __future__ import annotations

import asyncio
import hashlib
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
from proxbox_api.services import packer_plans
from proxbox_api.services.packer_plans import (
    PackerPlanError,
    PackerPlanPayload,
    acquire_operation_lease,
    finish_operation,
    issue_packer_plan,
    mark_operation_running,
    record_cancel_request,
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
    ["token", "endpoint", "credential", "target", "recipe", "expired"],
)
def test_signed_plan_rejects_tamper_drift_and_expiry(mutation: str) -> None:
    endpoint = _endpoint(token_name="automation", token_value="encrypted-token-a")
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
        verify_endpoint = _endpoint(
            ssh_host="93.184.216.35",
            token_name="automation",
            token_value="encrypted-token-a",
        )
    elif mutation == "credential":
        verify_endpoint = _endpoint(
            token_name="automation",
            token_value="encrypted-token-b",
        )
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


def test_endpoint_config_binding_uses_a_dedicated_keyed_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = _endpoint(
        password="encrypted-password-canary",
        token_name="automation",
        token_value="encrypted-token-canary",
    )
    contexts: list[str] = []

    def fake_derive(context: str) -> bytes:
        contexts.append(context)
        return f"test-key:{context}".encode()

    monkeypatch.setattr(packer_plans, "derive_service_signing_key", fake_derive)
    _plan, _digest, token = issue_packer_plan(
        endpoint=endpoint,
        target=_target(),
        recipe_digest="a" * 64,
        now=1000,
    )

    raw_digest = hashlib.sha256(
        packer_plans._canonical_json(packer_plans._endpoint_config_payload(endpoint))
    ).hexdigest()
    authenticated_digest = packer_plans.endpoint_config_digest(endpoint)

    assert authenticated_digest != raw_digest
    assert contexts == [
        "packer-endpoint-config-v1",
        "packer-preflight-v1",
        "packer-endpoint-config-v1",
    ]
    assert "encrypted-password-canary" not in token
    assert "encrypted-token-canary" not in token


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
        assert operation.lease_key == "17:9010"
        with pytest.raises(PackerPlanError, match="build_target_recovery_required"):
            await acquire_operation_lease(
                session,
                plan=second,
                plan_digest=second_digest,
                now=now,
            )

    await engine.dispose()


@pytest.mark.parametrize("terminal_kind", ["unknown", "cancelled"])
@pytest.mark.asyncio
async def test_unknown_and_cancelled_operations_retain_target_blocker(
    db_engine,
    terminal_kind: str,
) -> None:
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = time.time()
    first, first_digest, _ = issue_packer_plan(
        endpoint=_endpoint(), target=_target(), recipe_digest="a" * 64, now=now
    )
    second, second_digest, _ = issue_packer_plan(
        endpoint=_endpoint(), target=_target(), recipe_digest="a" * 64, now=now
    )

    async with factory() as session:
        operation = await acquire_operation_lease(
            session, plan=first, plan_digest=first_digest, now=now
        )
        if terminal_kind == "cancelled":
            await finish_operation(
                session,
                operation,
                state="cancelled",
                execution=CloudImageTemplateExecutionSummary(
                    attempted=True,
                    enabled=True,
                    cancellation_attempted=True,
                    cancellation_succeeded=True,
                ),
                verified=False,
                recovery_required=False,
                error_code="execution_cancelled",
            )
        else:
            await finish_operation(
                session,
                operation,
                state="recovery_required",
                execution=CloudImageTemplateExecutionSummary(attempted=True, enabled=True),
                verified=False,
                recovery_required=True,
                error_code="execution_unavailable",
            )

        assert operation.state == (
            "cancelled" if terminal_kind == "cancelled" else "recovery_required"
        )
        assert operation.recovery_required is True
        assert operation.lease_key == "17:9010"
        with pytest.raises(PackerPlanError, match="build_target_recovery_required"):
            await acquire_operation_lease(
                session,
                plan=second,
                plan_digest=second_digest,
                now=now,
            )

    await engine.dispose()


@pytest.mark.asyncio
async def test_expired_lease_becomes_recovery_blocker_and_rejects_concurrent_replacements(
    db_engine,
) -> None:
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    start = time.time()
    first, first_digest, _ = issue_packer_plan(
        endpoint=_endpoint(), target=_target(), recipe_digest="a" * 64, now=start
    )

    async with factory() as session:
        operation = await acquire_operation_lease(
            session, plan=first, plan_digest=first_digest, now=start
        )
        operation.lease_expires_at = start - 1
        session.add(operation)
        await session.commit()

    attempt_time = start + 1
    replacement_plans = [
        issue_packer_plan(
            endpoint=_endpoint(),
            target=_target(),
            recipe_digest="a" * 64,
            now=attempt_time,
        )[:2]
        for _ in range(2)
    ]

    async def attempt_replacement(plan_and_digest: tuple[PackerPlanPayload, str]) -> str:
        plan, digest = plan_and_digest
        async with factory() as session:
            with pytest.raises(PackerPlanError) as exc:
                await acquire_operation_lease(
                    session,
                    plan=plan,
                    plan_digest=digest,
                    now=attempt_time,
                )
            return exc.value.code

    assert await asyncio.gather(*(attempt_replacement(value) for value in replacement_plans)) == [
        "build_target_recovery_required",
        "build_target_recovery_required",
    ]

    async with factory() as session:
        operation = await session.get(CloudImageBuildOperation, first.plan_id)
        assert operation is not None
        assert operation.state == "recovery_required"
        assert operation.recovery_required is True
        assert operation.error_code == "execution_lease_expired"
        assert operation.lease_key == "17:9010"
        assert operation.finished_at is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_cancel_and_completion_use_compare_and_swap_journal_transitions(db_engine) -> None:
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = time.time()

    async def create_running(vmid: int) -> PackerPlanPayload:
        plan, digest, _ = issue_packer_plan(
            endpoint=_endpoint(),
            target=_target(vmid=vmid),
            recipe_digest="a" * 64,
            now=now,
        )
        async with factory() as creator:
            operation = await acquire_operation_lease(
                creator,
                plan=plan,
                plan_digest=digest,
                now=now,
            )
            await mark_operation_running(creator, operation)
        return plan

    completion_first = await create_running(9011)
    async with factory() as completion_session, factory() as stale_cancel_session:
        completion_row = await completion_session.get(
            CloudImageBuildOperation, completion_first.plan_id
        )
        stale_cancel_row = await stale_cancel_session.get(
            CloudImageBuildOperation, completion_first.plan_id
        )
        assert completion_row is not None and stale_cancel_row is not None
        assert await finish_operation(
            completion_session,
            completion_row,
            state="completed",
            execution=CloudImageTemplateExecutionSummary(attempted=True, enabled=True, exit_code=0),
            verified=True,
            recovery_required=False,
            error_code=None,
        )
        assert not await record_cancel_request(
            stale_cancel_session,
            stale_cancel_row,
            cancellation_succeeded=True,
        )
        assert stale_cancel_row.state == "completed"
        assert stale_cancel_row.lease_key is None

    cancel_first = await create_running(9012)
    async with factory() as cancel_session, factory() as stale_completion_session:
        cancel_row = await cancel_session.get(CloudImageBuildOperation, cancel_first.plan_id)
        stale_completion_row = await stale_completion_session.get(
            CloudImageBuildOperation, cancel_first.plan_id
        )
        assert cancel_row is not None and stale_completion_row is not None
        assert await record_cancel_request(
            cancel_session,
            cancel_row,
            cancellation_succeeded=True,
        )
        assert not await finish_operation(
            stale_completion_session,
            stale_completion_row,
            state="completed",
            execution=CloudImageTemplateExecutionSummary(attempted=True, enabled=True, exit_code=0),
            verified=True,
            recovery_required=False,
            error_code=None,
        )
        assert stale_completion_row.state == "recovery_required"
        assert stale_completion_row.recovery_required is True
        assert stale_completion_row.lease_key == "17:9012"

    await engine.dispose()


@pytest.mark.asyncio
async def test_bound_execution_rejects_concurrent_endpoint_authority_edit(
    db_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    endpoint = _endpoint(id=None, token_name="automation", token_value="encrypted-token-a")
    created_api_hosts: list[str] = []
    execution_called = False

    class FakeProxmox:
        async def aclose(self) -> None:
            return None

    async def fake_create(schema: object, *, initialize_metadata: bool = True) -> FakeProxmox:
        created_api_hosts.append(str(getattr(schema, "ip_address")))
        assert initialize_metadata is False
        return FakeProxmox()

    async def fail_execute(*_args: object, **_kwargs: object) -> None:
        nonlocal execution_called
        execution_called = True

    monkeypatch.setattr(template_images.ProxmoxSession, "create", staticmethod(fake_create))
    monkeypatch.setattr(template_images, "execute_pipeline_response", fail_execute)

    async with factory() as session:
        session.add(endpoint)
        await session.commit()
        await session.refresh(endpoint)
        request = CloudImageTemplateBuildRequest(
            endpoint_id=endpoint.id,
            target_node="pve01",
            vmid=9010,
            name="stale-authority-test",
            product_type="pfsense",
            product_version="2.8.1",
            provider="release_image",
            image_storage="local",
            vm_storage="local-zfs",
            snippets_storage="local",
            execute=True,
        )
        target, recipe_digest = pipeline_scripts.pipeline_execution_contract(request)
        _plan, _digest, token = issue_packer_plan(
            endpoint=endpoint,
            target=target,
            recipe_digest=recipe_digest,
        )
        request = request.model_copy(update={"preflight_plan_token": token})

        async with factory() as editor:
            current = await editor.get(ProxmoxEndpoint, endpoint.id)
            assert current is not None
            current.ip_address = "93.184.216.99"
            current.ssh_host = "93.184.216.99"
            current.token_value = "encrypted-token-b"
            editor.add(current)
            await editor.commit()

        with pytest.raises(template_images.HTTPException) as exc:
            await template_images._execute_bound_pipeline(request, session, endpoint)

        assert exc.value.detail["code"] == "preflight_plan_mismatch"
        assert created_api_hosts == ["93.184.216.99"]
        assert execution_called is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_bound_execution_rechecks_endpoint_after_preflight(
    db_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    endpoint = _endpoint(id=None, token_name="automation", token_value="encrypted-token-a")
    execution_called = False

    class FakeProxmox:
        async def aclose(self) -> None:
            return None

    async def fake_resolve(*_args: object, **_kwargs: object):
        return endpoint, FakeProxmox()

    async def edit_during_preflight(*_args: object, **_kwargs: object):
        async with factory() as editor:
            current = await editor.get(ProxmoxEndpoint, endpoint.id)
            assert current is not None
            current.ssh_host = "93.184.216.88"
            current.token_value = "encrypted-token-b"
            editor.add(current)
            await editor.commit()
        return CloudImageTemplatePreflightResponse(
            endpoint_id=int(endpoint.id or 0),
            target_node="pve01",
            vmid=9010,
            ready=True,
            writes_enabled=True,
            recipe_digest=recipe_digest,
        )

    async def fail_execute(*_args: object, **_kwargs: object) -> None:
        nonlocal execution_called
        execution_called = True

    monkeypatch.setattr(template_images, "_resolve_preflight_target", fake_resolve)
    monkeypatch.setattr(template_images, "run_packer_preflight", edit_during_preflight)
    monkeypatch.setattr(template_images, "execute_pipeline_response", fail_execute)

    async with factory() as session:
        session.add(endpoint)
        await session.commit()
        await session.refresh(endpoint)
        request = CloudImageTemplateBuildRequest(
            endpoint_id=endpoint.id,
            target_node="pve01",
            vmid=9010,
            name="post-preflight-authority-test",
            product_type="pfsense",
            product_version="2.8.1",
            provider="release_image",
            image_storage="local",
            vm_storage="local-zfs",
            snippets_storage="local",
            execute=True,
        )
        target, recipe_digest = pipeline_scripts.pipeline_execution_contract(request)
        _plan, _digest, token = issue_packer_plan(
            endpoint=endpoint,
            target=target,
            recipe_digest=recipe_digest,
        )
        request = request.model_copy(update={"preflight_plan_token": token})

        with pytest.raises(template_images.HTTPException) as exc:
            await template_images._execute_bound_pipeline(request, session, endpoint)

        assert exc.value.detail["code"] == "preflight_plan_mismatch"
        assert execution_called is False

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
        expected_lease = None if artifact_verified else f"{endpoint.id}:9010"
        assert operation.lease_key == expected_lease
        assert operation.stdout_bytes == 0
        assert operation.stderr_bytes == 0

        with pytest.raises(template_images.HTTPException) as exc:
            await template_images._execute_bound_pipeline(request, session, endpoint)
        assert exc.value.detail["code"] == "preflight_plan_already_consumed"

    await engine.dispose()


@pytest.mark.parametrize("cancellation_count", [2, 3])
@pytest.mark.asyncio
async def test_repeated_cancellation_completes_recovery_journal_and_retains_lease(
    db_engine,
    monkeypatch: pytest.MonkeyPatch,
    cancellation_count: int,
) -> None:
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    endpoint = _endpoint(id=None)
    journal_entered = asyncio.Event()
    release_journal = asyncio.Event()
    original_finish = finish_operation

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

    async def cancel_execution(*_args: object, **_kwargs: object):
        raise pipeline_scripts.PipelineExecutionCancelled(
            CloudImageTemplateExecutionSummary(
                attempted=True,
                enabled=True,
                cancellation_attempted=True,
                cancellation_succeeded=True,
            )
        )

    async def gated_finish(*args: object, **kwargs: object) -> None:
        journal_entered.set()
        await release_journal.wait()
        await original_finish(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(template_images, "_resolve_preflight_target", fake_resolve)
    monkeypatch.setattr(template_images, "run_packer_preflight", fake_preflight)
    monkeypatch.setattr(template_images, "execute_pipeline_response", cancel_execution)
    monkeypatch.setattr(template_images, "finish_operation", gated_finish)

    async with factory() as session:
        session.add(endpoint)
        await session.commit()
        await session.refresh(endpoint)
        request = CloudImageTemplateBuildRequest(
            endpoint_id=endpoint.id,
            target_node="pve01",
            vmid=9010,
            name="cancelled-template",
            product_type="pfsense",
            product_version="2.8.1",
            provider="release_image",
            image_storage="local",
            vm_storage="local-zfs",
            snippets_storage="local",
            execute=True,
        )
        target, recipe_digest = pipeline_scripts.pipeline_execution_contract(request)
        plan, _plan_digest, token = issue_packer_plan(
            endpoint=endpoint,
            target=target,
            recipe_digest=recipe_digest,
        )
        request = request.model_copy(update={"preflight_plan_token": token})
        task = asyncio.create_task(
            template_images._execute_bound_pipeline(request, session, endpoint)
        )
        await journal_entered.wait()
        for _ in range(cancellation_count):
            task.cancel()
            await asyncio.sleep(0)
        assert task.done() is False
        release_journal.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        operation = await session.get(CloudImageBuildOperation, plan.plan_id)
        assert operation is not None
        await session.refresh(operation)
        assert operation.state == "recovery_required"
        assert operation.error_code == "execution_cancelled"
        assert operation.recovery_required is True
        assert operation.lease_key == f"{endpoint.id}:9010"

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


def test_recipe_binding_is_keyed_and_not_a_low_entropy_secret_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = "dictionary-password-1234"
    request = CloudImageTemplateBuildRequest(
        endpoint_id=17,
        target_node="pve01",
        vmid=9010,
        name="secret-recipe",
        product_type="pfsense",
        product_version="custom",
        provider="release_image",
        image_url="https://93.184.216.34/image.qcow2?sig=low-entropy-token",
        user_data_yaml=f"#cloud-config\npassword: {canary}\n",
    )
    contexts: list[str] = []

    def fake_derive(context: str) -> bytes:
        contexts.append(context)
        return f"test-key:{context}".encode()

    monkeypatch.setattr(pipeline_scripts, "derive_service_signing_key", fake_derive)
    *_inputs, rendered_script, _commands, _url = pipeline_scripts._render_pipeline(request)
    raw_dictionary_digest = hashlib.sha256(rendered_script.encode()).hexdigest()

    opaque_digest = pipeline_scripts.pipeline_recipe_digest(request)
    response = pipeline_scripts.build_pipeline_response(request)
    changed_digest = pipeline_scripts.pipeline_recipe_digest(
        request.model_copy(
            update={"user_data_yaml": "#cloud-config\npassword: dictionary-password-1235\n"}
        )
    )

    assert opaque_digest == response.recipe_digest
    assert opaque_digest != raw_dictionary_digest
    assert opaque_digest != changed_digest
    assert set(contexts) == {"packer-recipe-binding-v1"}
    assert canary not in response.model_dump_json()
    assert "low-entropy-token" not in response.model_dump_json()


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


@pytest.mark.asyncio
async def test_cancel_route_journals_even_when_the_stop_task_is_force_cancelled(
    db_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A force-cancelled remote-stop task must not skip the durable journal.

    Regression: the cancel route called ``cancel_task.result()`` inside its
    ``except CancelledError`` branch without checking ``task.cancelled()``.
    When the inner stop task was itself genuinely cancelled (event-loop
    shutdown force-cancels independently created tasks), ``.result()``
    re-raised immediately and the durable cancel-journal write never ran —
    contradicting the documented guarantee that journal updates complete
    through repeated cancellation on this exact route.
    """
    async_url = str(db_engine.url).replace("sqlite:///", "sqlite+aiosqlite:///")
    engine = create_async_engine(async_url, connect_args={"check_same_thread": False})
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    endpoint = _endpoint(id=None)
    journal_calls: list[dict[str, object]] = []

    async def force_cancelled_stop(*_args: object, **_kwargs: object) -> bool:
        # A coroutine that raises CancelledError marks its task cancelled —
        # the same terminal state a force-cancel during loop shutdown yields.
        raise asyncio.CancelledError

    async def fake_gate(_session: object, _endpoint_id: object):
        return endpoint

    async def fake_ssh_gate(*_args: object, **_kwargs: object) -> None:
        return None

    def fake_ssh_target(*_args: object, **_kwargs: object) -> object:
        return object()

    async def spy_record(
        _session: object,
        operation_row: object,
        *,
        cancellation_succeeded: bool,
    ) -> bool:
        journal_calls.append(
            {
                "operation_id": getattr(operation_row, "id", None),
                "cancellation_succeeded": cancellation_succeeded,
            }
        )
        return True

    monkeypatch.setattr(template_images, "cancel_pipeline_operation", force_cancelled_stop)
    monkeypatch.setattr(template_images, "_gate", fake_gate)
    monkeypatch.setattr(template_images, "gate_ssh_access", fake_ssh_gate)
    monkeypatch.setattr(template_images, "_resolve_execution_ssh_target", fake_ssh_target)
    monkeypatch.setattr(template_images, "_record_cancel_request_durably", spy_record)

    async with factory() as session:
        session.add(endpoint)
        await session.commit()
        await session.refresh(endpoint)
        operation = CloudImageBuildOperation(
            id="cancel-route-force-cancel",
            plan_digest="d" * 64,
            recipe_digest="e" * 64,
            endpoint_config_digest="f" * 64,
            endpoint_id=int(endpoint.id or 0),
            target_node="pve01",
            vmid=9010,
            provider="release_image",
            state="running",
            lease_key=f"{endpoint.id}:9010",
            remote_unit="proxbox-image-build-cancel-route.service",
            plan_expires_at=time.time() + 300,
            lease_expires_at=time.time() + 3600,
            attempted=True,
        )
        session.add(operation)
        await session.commit()

        with pytest.raises(asyncio.CancelledError):
            await template_images.cancel_cloud_image_build_operation(
                operation.id,
                session,
            )

        # The durable journal write ran before the cancellation was re-raised,
        # and journalled a *failed* stop attempt: a force-cancelled stop task
        # proves nothing about the remote unit.
        assert journal_calls == [
            {
                "operation_id": operation.id,
                "cancellation_succeeded": False,
            }
        ]

    await engine.dispose()

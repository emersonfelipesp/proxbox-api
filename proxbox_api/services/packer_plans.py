"""Signed preflight plans and durable Cloud Image Pipeline operation leases."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from proxbox_api.credentials import derive_service_signing_key
from proxbox_api.database import CloudImageBuildOperation, ProxmoxEndpoint
from proxbox_api.schemas.cloud_provision import (
    CloudImageBuildOperationResponse,
    CloudImageBuildTarget,
    CloudImageTemplateExecutionSummary,
)

_PLAN_TTL_SECONDS = 300
_EXECUTION_LEASE_SECONDS = 3660
_PLAN_SIGNING_CONTEXT = "packer-preflight-v1"


class PackerPlanError(ValueError):
    """Fixed-code plan verification or lease failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PackerPlanPayload(BaseModel):
    """Secret-free claims authenticated by a preflight plan token."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["1.0"] = "1.0"
    plan_id: str
    endpoint_id: int
    endpoint_config_digest: str
    target_node: str
    vmid: int
    provider: str
    image_storage: str
    vm_storage: str
    snippets_storage: str | None
    recipe_digest: str
    issued_at: float
    expires_at: float


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def endpoint_config_digest(endpoint: ProxmoxEndpoint) -> str:
    """Hash every persisted field that can affect API or SSH execution authority."""

    payload = {
        "id": endpoint.id,
        "name": endpoint.name,
        "ip_address": endpoint.ip_address,
        "domain": endpoint.domain,
        "port": endpoint.port,
        "username": endpoint.username,
        "password": endpoint.password,
        "token_name": endpoint.token_name,
        "token_value": endpoint.token_value,
        "verify_ssl": endpoint.verify_ssl,
        "allow_writes": endpoint.allow_writes,
        "access_methods": endpoint.access_methods,
        "enabled": endpoint.enabled,
        "ssh_target_node": endpoint.ssh_target_node,
        "ssh_host": endpoint.ssh_host,
        "ssh_username": endpoint.ssh_username,
        "ssh_port": endpoint.ssh_port,
        "ssh_identity_file": endpoint.ssh_identity_file,
        "ssh_known_host_fingerprint": endpoint.ssh_known_host_fingerprint,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def issue_packer_plan(
    *,
    endpoint: ProxmoxEndpoint,
    target: CloudImageBuildTarget,
    recipe_digest: str,
    now: float | None = None,
) -> tuple[PackerPlanPayload, str, str]:
    """Return ``(claims, plan_digest, signed_token)`` without database mutation."""

    issued_at = time.time() if now is None else now
    claims = PackerPlanPayload(
        plan_id=str(uuid.uuid4()),
        endpoint_id=int(endpoint.id or 0),
        endpoint_config_digest=endpoint_config_digest(endpoint),
        target_node=target.target_node,
        vmid=target.vmid,
        provider=target.provider.value,
        image_storage=target.image_storage,
        vm_storage=target.vm_storage,
        snippets_storage=target.snippets_storage,
        recipe_digest=recipe_digest,
        issued_at=issued_at,
        expires_at=issued_at + _PLAN_TTL_SECONDS,
    )
    encoded_claims = _canonical_json(claims.model_dump(mode="json"))
    signature = hmac.new(
        derive_service_signing_key(_PLAN_SIGNING_CONTEXT),
        encoded_claims,
        hashlib.sha256,
    ).digest()
    token = f"{_b64encode(encoded_claims)}.{_b64encode(signature)}"
    return claims, hashlib.sha256(encoded_claims).hexdigest(), token


def verify_packer_plan(
    token: str,
    *,
    endpoint: ProxmoxEndpoint,
    target: CloudImageBuildTarget,
    recipe_digest: str,
    now: float | None = None,
) -> tuple[PackerPlanPayload, str]:
    """Authenticate a plan and bind it to current endpoint and recipe state."""

    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload_bytes = _b64decode(encoded_payload)
        supplied_signature = _b64decode(encoded_signature)
    except (ValueError, TypeError):
        raise PackerPlanError("preflight_plan_invalid") from None

    expected_signature = hmac.new(
        derive_service_signing_key(_PLAN_SIGNING_CONTEXT),
        payload_bytes,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise PackerPlanError("preflight_plan_invalid")

    try:
        payload = PackerPlanPayload.model_validate_json(payload_bytes)
        uuid.UUID(payload.plan_id)
    except (ValidationError, ValueError):
        raise PackerPlanError("preflight_plan_invalid") from None

    current_time = time.time() if now is None else now
    if payload.expires_at <= current_time:
        raise PackerPlanError("preflight_plan_expired")
    if payload.issued_at > current_time + 30:
        raise PackerPlanError("preflight_plan_invalid")

    expected = {
        "endpoint_id": int(endpoint.id or 0),
        "endpoint_config_digest": endpoint_config_digest(endpoint),
        "target_node": target.target_node,
        "vmid": target.vmid,
        "provider": target.provider.value,
        "image_storage": target.image_storage,
        "vm_storage": target.vm_storage,
        "snippets_storage": target.snippets_storage,
        "recipe_digest": recipe_digest,
    }
    actual = {field: getattr(payload, field) for field in expected}
    if not hmac.compare_digest(
        hashlib.sha256(_canonical_json(actual)).digest(),
        hashlib.sha256(_canonical_json(expected)).digest(),
    ):
        raise PackerPlanError("preflight_plan_mismatch")
    return payload, hashlib.sha256(payload_bytes).hexdigest()


async def acquire_operation_lease(
    session: AsyncSession,
    *,
    plan: PackerPlanPayload,
    plan_digest: str,
    now: float | None = None,
) -> CloudImageBuildOperation:
    """Consume one plan and atomically acquire its endpoint/VMID lease."""

    current_time = time.time() if now is None else now
    if plan.expires_at <= current_time:
        raise PackerPlanError("preflight_plan_expired")
    replay = await session.get(CloudImageBuildOperation, plan.plan_id)
    if replay is not None:
        raise PackerPlanError("preflight_plan_already_consumed")

    lease_key = f"{plan.endpoint_id}:{plan.vmid}"
    existing_result = await session.exec(
        select(CloudImageBuildOperation).where(CloudImageBuildOperation.lease_key == lease_key)
    )
    existing = existing_result.first()
    if existing is not None:
        if existing.lease_expires_at > current_time:
            raise PackerPlanError("build_target_leased")
        existing.state = "recovery_required"
        existing.recovery_required = True
        existing.error_code = "execution_lease_expired"
        existing.lease_key = None
        existing.finished_at = current_time
        existing.updated_at = current_time
        session.add(existing)
        await session.commit()

    operation = CloudImageBuildOperation(
        id=plan.plan_id,
        plan_digest=plan_digest,
        recipe_digest=plan.recipe_digest,
        endpoint_config_digest=plan.endpoint_config_digest,
        endpoint_id=plan.endpoint_id,
        target_node=plan.target_node,
        vmid=plan.vmid,
        provider=plan.provider,
        state="leased",
        lease_key=lease_key,
        remote_unit=f"proxbox-cloud-image-{plan.plan_id}",
        plan_expires_at=plan.expires_at,
        lease_expires_at=current_time + _EXECUTION_LEASE_SECONDS,
        created_at=current_time,
        updated_at=current_time,
    )
    session.add(operation)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise PackerPlanError("build_target_leased") from None
    await session.refresh(operation)
    return operation


async def mark_operation_running(
    session: AsyncSession,
    operation: CloudImageBuildOperation,
) -> None:
    now = time.time()
    operation.state = "running"
    operation.attempted = True
    operation.started_at = now
    operation.updated_at = now
    session.add(operation)
    await session.commit()


async def finish_operation(
    session: AsyncSession,
    operation: CloudImageBuildOperation,
    *,
    state: Literal["completed", "failed", "cancelled", "recovery_required"],
    execution: CloudImageTemplateExecutionSummary,
    verified: bool,
    recovery_required: bool,
    error_code: str | None,
) -> None:
    now = time.time()
    operation.state = state
    operation.lease_key = None
    operation.exit_code = execution.exit_code
    operation.stdout_bytes = execution.stdout_bytes
    operation.stderr_bytes = execution.stderr_bytes
    operation.stdout_lines = execution.stdout_lines
    operation.stderr_lines = execution.stderr_lines
    operation.verified = verified
    operation.recovery_required = recovery_required
    operation.cancel_requested = operation.cancel_requested or execution.cancellation_attempted
    operation.cancellation_succeeded = execution.cancellation_succeeded
    operation.error_code = error_code
    operation.finished_at = now
    operation.updated_at = now
    session.add(operation)
    await session.commit()


async def record_cancel_request(
    session: AsyncSession,
    operation: CloudImageBuildOperation,
    *,
    cancellation_succeeded: bool,
) -> None:
    """Journal an operator cancellation without prematurely releasing its lease."""

    now = time.time()
    operation.cancel_requested = True
    operation.cancellation_succeeded = cancellation_succeeded
    operation.recovery_required = True
    operation.state = "recovery_required"
    operation.error_code = "execution_cancel_requested"
    operation.updated_at = now
    session.add(operation)
    await session.commit()


def operation_response(operation: CloudImageBuildOperation) -> CloudImageBuildOperationResponse:
    """Convert a journal row without exposing internal lease or endpoint material."""

    return CloudImageBuildOperationResponse.model_validate(
        {
            "operation_id": operation.id,
            "endpoint_id": operation.endpoint_id,
            "target_node": operation.target_node,
            "vmid": operation.vmid,
            "provider": operation.provider,
            "state": operation.state,
            "recipe_digest": operation.recipe_digest,
            "plan_digest": operation.plan_digest,
            "verified": operation.verified,
            "recovery_required": operation.recovery_required,
            "cancel_requested": operation.cancel_requested,
            "cancellation_succeeded": operation.cancellation_succeeded,
            "error_code": operation.error_code,
            "created_at": operation.created_at,
            "started_at": operation.started_at,
            "finished_at": operation.finished_at,
            "updated_at": operation.updated_at,
        }
    )

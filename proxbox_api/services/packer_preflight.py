"""Read-only, endpoint-scoped preflight for the Cloud Image Build Pipeline."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

from proxbox_api.schemas.cloud_provision import (
    CloudImageTemplatePreflightRequest,
    CloudImageTemplatePreflightResponse,
    PackerFinding,
    PackerFindingSeverity,
    PackerPreflightCapability,
    PackerPreflightCapabilityResult,
    PackerPreflightCapabilityStatus,
)
from proxbox_api.session.proxmox import ProxmoxSession
from proxbox_api.utils.async_compat import maybe_await


def _mapping(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python", by_alias=True, exclude_none=True)
        if isinstance(dumped, Mapping):
            return {str(key): item for key, item in dumped.items()}
    return {}


def _rows(value: object) -> list[dict[str, object]] | None:
    """Return a validated collection, preserving invalid-vs-empty semantics.

    Collection endpoints legitimately return an empty list. A scalar, malformed
    envelope, or malformed row is different: readiness cannot be proven from it
    and callers must report the corresponding capability as unsupported.
    """

    missing = object()
    root = getattr(value, "root", missing)
    if root is not missing:
        value = root
    else:
        mapping = _mapping(value)
        if isinstance(value, Mapping) or mapping:
            if "data" not in mapping:
                return None
            value = mapping["data"]

    if not isinstance(value, (list, tuple)):
        return None

    rows: list[dict[str, object]] = []
    for item in value:
        record = _mapping(item)
        if not record:
            return None
        rows.append(record)
    return rows


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on", "online", "active"}


def _content_types(value: object) -> set[str] | None:
    if isinstance(value, (list, tuple, set, frozenset)):
        return {str(item).strip().lower() for item in value if str(item).strip()}
    if not isinstance(value, str) or not value.strip():
        return None
    return {item.strip().lower() for item in re.split(r"[\s,;]+", value) if item.strip()}


def _add_result(
    capabilities: list[PackerPreflightCapabilityResult],
    findings: list[PackerFinding],
    *,
    capability: PackerPreflightCapability,
    status: PackerPreflightCapabilityStatus,
    target: str,
    code: str,
    severity: PackerFindingSeverity,
    message: str,
) -> None:
    capabilities.append(
        PackerPreflightCapabilityResult(
            capability=capability,
            status=status,
            target=target,
        )
    )
    findings.append(
        PackerFinding(
            code=code,
            severity=severity,
            target=target,
            message=message,
        )
    )


def _check_node(
    request: CloudImageTemplatePreflightRequest,
    rows: list[dict[str, object]],
    capabilities: list[PackerPreflightCapabilityResult],
    findings: list[PackerFinding],
) -> None:
    target = f"node:{request.target_node}"
    node = next(
        (
            row
            for row in rows
            if str(row.get("name") or row.get("node") or "") == request.target_node
            and str(row.get("type") or "node").lower() == "node"
        ),
        None,
    )
    if node is None:
        _add_result(
            capabilities,
            findings,
            capability=PackerPreflightCapability.node_online,
            status=PackerPreflightCapabilityStatus.failed,
            target=target,
            code="node_not_found",
            severity=PackerFindingSeverity.error,
            message="The target node was not returned by the selected endpoint.",
        )
        return
    if not _truthy(node.get("online", node.get("status"))):
        _add_result(
            capabilities,
            findings,
            capability=PackerPreflightCapability.node_online,
            status=PackerPreflightCapabilityStatus.failed,
            target=target,
            code="node_offline",
            severity=PackerFindingSeverity.error,
            message="The target node is not online.",
        )
        return
    _add_result(
        capabilities,
        findings,
        capability=PackerPreflightCapability.node_online,
        status=PackerPreflightCapabilityStatus.passed,
        target=target,
        code="node_online",
        severity=PackerFindingSeverity.info,
        message="The target node is online.",
    )


def _check_storage(
    *,
    storage_rows: list[dict[str, object]],
    storage_name: str,
    required_content: str,
    capability: PackerPreflightCapability,
    capabilities: list[PackerPreflightCapabilityResult],
    findings: list[PackerFinding],
) -> None:
    target = f"storage:{storage_name}"
    storage = next(
        (row for row in storage_rows if str(row.get("storage") or "") == storage_name),
        None,
    )
    if storage is None:
        _add_result(
            capabilities,
            findings,
            capability=capability,
            status=PackerPreflightCapabilityStatus.failed,
            target=target,
            code="storage_not_found",
            severity=PackerFindingSeverity.error,
            message="The required storage was not returned for the target node.",
        )
        return
    if "enabled" in storage and not _truthy(storage["enabled"]):
        _add_result(
            capabilities,
            findings,
            capability=capability,
            status=PackerPreflightCapabilityStatus.failed,
            target=target,
            code="storage_disabled",
            severity=PackerFindingSeverity.error,
            message="The required storage is disabled on the target node.",
        )
        return
    if "active" in storage and not _truthy(storage["active"]):
        _add_result(
            capabilities,
            findings,
            capability=capability,
            status=PackerPreflightCapabilityStatus.failed,
            target=target,
            code="storage_inactive",
            severity=PackerFindingSeverity.error,
            message="The required storage is not active on the target node.",
        )
        return
    if "enabled" not in storage or "active" not in storage:
        _add_result(
            capabilities,
            findings,
            capability=capability,
            status=PackerPreflightCapabilityStatus.unsupported,
            target=target,
            code="storage_state_check_unsupported",
            severity=PackerFindingSeverity.warning,
            message="The endpoint did not expose both enabled and active storage state.",
        )
        return
    content_types = _content_types(storage.get("content"))
    if content_types is None:
        _add_result(
            capabilities,
            findings,
            capability=capability,
            status=PackerPreflightCapabilityStatus.unsupported,
            target=target,
            code="storage_content_check_unsupported",
            severity=PackerFindingSeverity.warning,
            message="The endpoint did not expose storage content capabilities.",
        )
        return
    if required_content not in content_types:
        _add_result(
            capabilities,
            findings,
            capability=capability,
            status=PackerPreflightCapabilityStatus.failed,
            target=target,
            code="storage_content_missing",
            severity=PackerFindingSeverity.error,
            message=f"The storage does not support required content type '{required_content}'.",
        )
        return
    _add_result(
        capabilities,
        findings,
        capability=capability,
        status=PackerPreflightCapabilityStatus.passed,
        target=target,
        code="storage_compatible",
        severity=PackerFindingSeverity.info,
        message=f"The storage supports required content type '{required_content}'.",
    )


async def _read_node_status(
    request: CloudImageTemplatePreflightRequest,
    api: Any,
    capabilities: list[PackerPreflightCapabilityResult],
    findings: list[PackerFinding],
) -> None:
    try:
        node_payload = await maybe_await(api("cluster/status").get())
    except Exception:  # noqa: BLE001 - upstream support is reported as a typed finding
        _add_result(
            capabilities,
            findings,
            capability=PackerPreflightCapability.node_online,
            status=PackerPreflightCapabilityStatus.unsupported,
            target=f"node:{request.target_node}",
            code="node_check_unsupported",
            severity=PackerFindingSeverity.warning,
            message="The endpoint could not provide a read-only node-status check.",
        )
        return

    node_rows = _rows(node_payload)
    if node_rows is None:
        _add_result(
            capabilities,
            findings,
            capability=PackerPreflightCapability.node_online,
            status=PackerPreflightCapabilityStatus.unsupported,
            target=f"node:{request.target_node}",
            code="node_payload_invalid",
            severity=PackerFindingSeverity.warning,
            message="The endpoint returned an invalid node-status collection.",
        )
        return
    _check_node(request, node_rows, capabilities, findings)


async def _read_storage_status(
    request: CloudImageTemplatePreflightRequest,
    api: Any,
    capabilities: list[PackerPreflightCapabilityResult],
    findings: list[PackerFinding],
) -> None:
    target = request.build_target()
    capability_by_role_content = {
        ("image", "images"): PackerPreflightCapability.image_storage_images,
        ("image", "iso"): PackerPreflightCapability.image_storage_iso,
        ("vm", "images"): PackerPreflightCapability.vm_storage_images,
        ("snippets", "snippets"): PackerPreflightCapability.snippets_storage_snippets,
    }
    storage_checks = tuple(
        (storage_name, required_content, capability_by_role_content[(role, required_content)])
        for role, storage_name, required_content in target.storage_requirements()
    )
    try:
        storage_payload = await maybe_await(api.nodes(request.target_node).storage.get())
    except Exception:  # noqa: BLE001 - do not expose upstream exception or credentials
        code = "storage_check_unsupported"
        message = "The endpoint could not provide a read-only storage capability check."
    else:
        storage_rows = _rows(storage_payload)
        if storage_rows is not None:
            for storage_name, required_content, capability in storage_checks:
                _check_storage(
                    storage_rows=storage_rows,
                    storage_name=storage_name,
                    required_content=required_content,
                    capability=capability,
                    capabilities=capabilities,
                    findings=findings,
                )
            return
        code = "storage_payload_invalid"
        message = "The endpoint returned an invalid storage collection."

    for storage_name, _required_content, capability in storage_checks:
        _add_result(
            capabilities,
            findings,
            capability=capability,
            status=PackerPreflightCapabilityStatus.unsupported,
            target=f"storage:{storage_name}",
            code=code,
            severity=PackerFindingSeverity.warning,
            message=message,
        )


async def _read_vmid_status(
    request: CloudImageTemplatePreflightRequest,
    api: Any,
    capabilities: list[PackerPreflightCapabilityResult],
    findings: list[PackerFinding],
) -> None:
    target = f"vmid:{request.vmid}"
    try:
        nextid_payload = await maybe_await(api("cluster/nextid").get(vmid=request.vmid))
    except Exception:  # noqa: BLE001 - do not expose upstream exception or credentials
        in_use = False
        try:
            resource_payload = await maybe_await(api("cluster/resources").get(type="vm"))
            resource_rows = _rows(resource_payload)
            in_use = bool(
                resource_rows is not None
                and any(str(row.get("vmid") or "") == str(request.vmid) for row in resource_rows)
            )
        except Exception:  # noqa: BLE001 - enumeration is supplemental only
            pass
        _add_result(
            capabilities,
            findings,
            capability=PackerPreflightCapability.vmid_available,
            status=(
                PackerPreflightCapabilityStatus.failed
                if in_use
                else PackerPreflightCapabilityStatus.unsupported
            ),
            target=target,
            code="vmid_in_use" if in_use else "vmid_check_unsupported",
            severity=PackerFindingSeverity.error if in_use else PackerFindingSeverity.warning,
            message=(
                "The requested VMID is already in use."
                if in_use
                else "The endpoint could not authoritatively verify VMID availability."
            ),
        )
        return

    root = getattr(nextid_payload, "root", nextid_payload)
    mapped = _mapping(root)
    if mapped:
        root = mapped.get("data")
    if isinstance(root, bool) or not str(root or "").strip().isdigit():
        _add_result(
            capabilities,
            findings,
            capability=PackerPreflightCapability.vmid_available,
            status=PackerPreflightCapabilityStatus.unsupported,
            target=target,
            code="vmid_payload_invalid",
            severity=PackerFindingSeverity.warning,
            message="The endpoint returned an invalid authoritative nextid response.",
        )
        return

    if int(str(root)) != request.vmid:
        _add_result(
            capabilities,
            findings,
            capability=PackerPreflightCapability.vmid_available,
            status=PackerPreflightCapabilityStatus.unsupported,
            target=target,
            code="vmid_payload_invalid",
            severity=PackerFindingSeverity.warning,
            message="The endpoint returned a different VMID from the authoritative nextid check.",
        )
        return

    # ``cluster/resources`` is deliberately supplemental: RBAC can hide rows,
    # while ``cluster/nextid?vmid=`` is the authoritative allocation check.
    enumerated_in_use = False
    try:
        resource_payload = await maybe_await(api("cluster/resources").get(type="vm"))
        resource_rows = _rows(resource_payload)
        enumerated_in_use = bool(
            resource_rows is not None
            and any(str(row.get("vmid") or "") == str(request.vmid) for row in resource_rows)
        )
    except Exception:  # noqa: BLE001 - authoritative nextid result remains sufficient
        pass
    _add_result(
        capabilities,
        findings,
        capability=PackerPreflightCapability.vmid_available,
        status=(
            PackerPreflightCapabilityStatus.failed
            if enumerated_in_use
            else PackerPreflightCapabilityStatus.passed
        ),
        target=target,
        code="vmid_in_use" if enumerated_in_use else "vmid_available",
        severity=(PackerFindingSeverity.error if enumerated_in_use else PackerFindingSeverity.info),
        message=(
            "The requested VMID is already in use."
            if enumerated_in_use
            else "The requested VMID passed the authoritative nextid availability check."
        ),
    )


async def run_packer_preflight(
    request: CloudImageTemplatePreflightRequest,
    proxmox: ProxmoxSession,
    *,
    writes_enabled: bool,
) -> CloudImageTemplatePreflightResponse:
    """Run endpoint reads only; this function never calls a Proxmox write verb."""

    api = cast(Any, proxmox.session)

    capabilities = [
        PackerPreflightCapabilityResult(
            capability=PackerPreflightCapability.endpoint_session,
            status=PackerPreflightCapabilityStatus.passed,
            target=f"endpoint:{request.endpoint_id}",
        )
    ]
    findings = [
        PackerFinding(
            code="endpoint_session_exact",
            severity=PackerFindingSeverity.info,
            target=f"endpoint:{request.endpoint_id}",
            message="Exactly one enabled session matched the persisted endpoint.",
        )
    ]

    await _read_node_status(request, api, capabilities, findings)
    await _read_storage_status(request, api, capabilities, findings)
    await _read_vmid_status(request, api, capabilities, findings)

    ready = all(result.status == PackerPreflightCapabilityStatus.passed for result in capabilities)
    return CloudImageTemplatePreflightResponse(
        endpoint_id=request.endpoint_id,
        target_node=request.target_node,
        vmid=request.vmid,
        ready=ready,
        writes_enabled=writes_enabled,
        recipe_digest=request.recipe_digest,
        capabilities=capabilities,
        findings=findings,
    )

"""Proxmox-backed Ceph v2 provider adapter."""

from __future__ import annotations

import asyncio
from typing import Any

from proxbox_api.ceph.endpoint_binding import BoundProxmoxSession
from proxbox_api.ceph.routes import _client_for, _node_names, _session_host, _session_name
from proxbox_api.ceph.timing import CephTimingSettings
from proxbox_api.ceph.v2_providers.base import (
    CephCapabilityUnsupported,
    CephProviderAdapter,
    CephProviderBoundaryError,
    CephWriteGateDenied,
    ProviderDispatch,
    TaskHeartbeat,
    ceph_write_execution_enabled,
)
from proxbox_api.ceph.v2_providers.proxmox_writer import (
    SYNCHRONOUS_OPERATION_KINDS,
    cephwrite_importable,
    execute_operation,
    operation_kinds,
    resolve_node,
    validate_operation_payload,
)
from proxbox_api.ceph.v2_schemas import (
    DesiredObject,
    DesiredStateBundle,
    ProviderCapabilities,
    ProviderOperation,
)
from proxbox_api.database_protocols import DatabaseSessionProtocol
from proxbox_api.generated.proxmox.latest import pydantic_models as generated_models
from proxbox_api.logger import logger
from proxbox_api.proxmox_async import resolve_async
from proxbox_api.services.proxmox_helpers import get_node_task_status
from proxbox_api.session.proxmox_core import ProxmoxSession


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_plain(item) for item in value]
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _plain(value.model_dump(mode="json"))
    if hasattr(value, "dict") and callable(value.dict):
        return _plain(value.dict())
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {
            str(key): _plain(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return value


def _resource_name(payload: Any) -> str | None:
    plain = _plain(payload)
    if not isinstance(plain, dict):
        return None
    for key in ("name", "pool", "pool_name", "id", "rule_name", "fs_name"):
        value = plain.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _normalize_kind(kind: str) -> str:
    cleaned = kind.strip().lower().replace("-", "_")
    aliases = {
        "pools": "pool",
        "osds": "osd",
        "cephfs": "filesystem",
        "fs": "filesystem",
        "filesystems": "filesystem",
        "crush_rules": "crush_rule",
        "rules": "crush_rule",
    }
    return aliases.get(cleaned, cleaned)


def _desired_target(desired: DesiredObject) -> str:
    target = desired.target_ref or desired.name
    if target:
        return str(target)
    payload_name = _resource_name(desired.payload)
    return payload_name or ""


def _desired_node(desired: DesiredObject) -> str | None:
    """Read an explicit desired node without inventing a cluster fallback."""

    for source in (desired.payload, desired.metadata, desired.model_extra or {}):
        if isinstance(source, dict) and source.get("node") not in (None, ""):
            return str(source["node"])
    return None


def _payload_matches(desired_payload: dict[str, Any], live_summary: dict[str, Any]) -> bool:
    if not desired_payload:
        return True
    for key, value in desired_payload.items():
        if key not in live_summary:
            return False
        if _plain(live_summary[key]) != _plain(value):
            return False
    return True


async def _fresh_node_names(px: ProxmoxSession) -> list[str]:
    """Fetch current provider node membership without trusting session bootstrap cache."""

    try:
        session = px.session
        if session is None:
            raise RuntimeError("Proxmox SDK session is unavailable")
        raw_status = await resolve_async(session("cluster/status").get())
        status = generated_models.GetClusterStatusResponse.model_validate(raw_status).root
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - upstream errors can contain credentials
        failure = CephProviderBoundaryError(
            "provider_node_authority_unavailable",
            "Current Proxmox node membership could not be verified safely.",
        )
        logger.warning(
            "Ceph v2 node authority unavailable correlation_id=%s endpoint_id=%s",
            failure.correlation_id,
            getattr(px, "db_endpoint_id", None),
        )
        raise failure from None

    nodes: set[str] = set()
    for item in status:
        plain = _plain(item)
        if not isinstance(plain, dict):
            continue
        if plain.get("type") == "node" and plain.get("name"):
            nodes.add(str(plain["name"]))
    return sorted(nodes)


class ProxmoxCephProviderAdapter(CephProviderAdapter):
    """Ceph v2 adapter bound to one explicitly selected Proxmox endpoint."""

    provider = "proxmox"

    def __init__(
        self,
        read_sessions: list[ProxmoxSession] | None = None,
        *,
        bound_session: BoundProxmoxSession | None = None,
        database_session: DatabaseSessionProtocol | None = None,
        writes_authorized: bool = False,
        timing_settings: CephTimingSettings | None = None,
        task_poll_timeout: float | None = None,
        task_poll_interval: float | None = None,
    ) -> None:
        self._bound_session = bound_session
        self._database_session = database_session
        self._read_sessions = (
            [bound_session.raw_session()]
            if bound_session is not None
            else list(read_sessions or [])
        )
        self._writes_authorized = writes_authorized
        timing = timing_settings or CephTimingSettings()
        self._run_lease_seconds = timing.run_lease_seconds
        self._task_poll_timeout = max(
            0.0,
            timing.task_timeout if task_poll_timeout is None else task_poll_timeout,
        )
        requested_poll_interval = max(
            0.0,
            timing.task_poll_interval if task_poll_interval is None else task_poll_interval,
        )
        self._task_poll_interval = min(requested_poll_interval, self._task_poll_timeout)

    def _selected_session(self) -> ProxmoxSession:
        if not isinstance(self._bound_session, BoundProxmoxSession):
            raise CephWriteGateDenied(
                "endpoint_session_untagged",
                "Ceph writes require one privately bound local endpoint session.",
            )
        return self._bound_session.raw_session()

    @property
    def endpoint_id(self) -> int | None:
        return self._bound_session.endpoint_id if self._bound_session is not None else None

    @property
    def run_lease_seconds(self) -> float:
        """Return the immutable duration paired with this adapter request."""

        return self._run_lease_seconds

    async def capabilities(self) -> ProviderCapabilities:
        sdk_writes = cephwrite_importable()
        writes = (
            sdk_writes
            and ceph_write_execution_enabled()
            and isinstance(self._bound_session, BoundProxmoxSession)
            and self._database_session is not None
            and self._writes_authorized
        )
        kinds = operation_kinds(writes)
        if writes:
            notes = [
                "Proxmox Ceph v2 adapter reads, plans, and applies PVE-managed Ceph "
                "writes (pools, flags, OSD/MON/MGR/MDS lifecycle, CephFS) through the "
                "proxmox-sdk CephWrite helpers. Every mutation rechecks the persisted "
                "endpoint write gate."
            ]
        else:
            reasons: list[str] = []
            if self.endpoint_id is None:
                reasons.append("no endpoint_id was selected")
            elif not isinstance(self._bound_session, BoundProxmoxSession):
                reasons.append("the selected endpoint has no privately bound session")
            if not self._writes_authorized:
                reasons.append("the selected endpoint is disabled or allow_writes is false")
            if not sdk_writes:
                reasons.append("the installed proxmox-sdk has no CephWrite domain")
            if not ceph_write_execution_enabled():
                reasons.append("the Ceph write execution/trusted-gateway gate is disabled")
            notes = [
                "Proxmox Ceph writes are unavailable: "
                + "; ".join(reasons or ["write gate denied"])
            ]
        return ProviderCapabilities(
            provider=self.provider,
            endpoint_id=self.endpoint_id,
            supported=True,
            read_state=True,
            diff=True,
            plan=True,
            apply=writes,
            reconcile=True,
            metrics=True,
            operation_kinds=kinds,
            destructive_operations=writes,
            notes=notes,
        )

    async def read_state(self, scope: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        clusters: list[dict[str, Any]] = []
        resources: list[dict[str, Any]] = []
        errors: list[str] = []

        for px in self._read_sessions:
            cluster = {
                "provider": self.provider,
                "name": _session_name(px),
                "host": _session_host(px),
                "nodes": _node_names(px),
                "status": None,
                "metadata": None,
                "flags": [],
                "errors": [],
            }
            try:
                client = _client_for(px)
                cluster["status"] = _plain(await client.cluster.status())
                cluster["metadata"] = _plain(await client.cluster.metadata())
                cluster["flags"] = _plain(await client.cluster.flags())
                for flag in cluster["flags"]:
                    name = _resource_name(flag)
                    if name:
                        resources.append(
                            {
                                "kind": "flag",
                                "target_ref": name,
                                "summary": _plain(flag),
                                "cluster": cluster["name"],
                            }
                        )

                for node in cluster["nodes"]:
                    await self._read_node_resources(client, node, cluster["name"], resources)
            except Exception:  # noqa: BLE001
                failure = CephProviderBoundaryError(
                    "provider_state_unavailable",
                    "The Proxmox Ceph state could not be read safely.",
                )
                logger.warning(
                    "Ceph v2 state unavailable correlation_id=%s endpoint_id=%s",
                    failure.correlation_id,
                    self.endpoint_id,
                )
                raise failure from None
            clusters.append(cluster)

        return {
            "provider": self.provider,
            "clusters": clusters,
            "resources": resources,
            "errors": errors,
            "summary": {
                "clusters": len(clusters),
                "resources": len(resources),
                "errors": len(errors),
            },
        }

    async def _read_node_resources(
        self,
        client: Any,
        node: str,
        cluster_name: str,
        resources: list[dict[str, Any]],
    ) -> None:
        node_specs = (
            ("osd", client.nodes.osds),
            ("pool", client.nodes.pools),
            ("filesystem", client.nodes.filesystems),
            ("crush_rule", client.nodes.rules),
        )
        for kind, reader in node_specs:
            for item in _plain(await reader(node)):
                name = _resource_name(item)
                if name is None:
                    continue
                resources.append(
                    {
                        "kind": kind,
                        "target_ref": name,
                        "summary": _plain(item),
                        "cluster": cluster_name,
                        "node": node,
                    }
                )

    async def diff(  # noqa: C901 - explicit node ambiguity handling
        self,
        desired: DesiredStateBundle,
        live: dict[str, Any],
    ) -> list[ProviderOperation]:
        live_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in live.get("resources", []):
            if not isinstance(item, dict):
                continue
            key = (
                _normalize_kind(str(item.get("kind", ""))),
                str(item.get("target_ref") or ""),
            )
            live_index.setdefault(key, []).append(item)

        operations: list[ProviderOperation] = []
        for desired_object in desired.objects:
            kind = _normalize_kind(desired_object.kind)
            target_ref = _desired_target(desired_object)
            action = desired_object.action.strip().lower() or "ensure"
            requested_node = _desired_node(desired_object)
            candidates = live_index.get((kind, target_ref), [])
            node_candidates = {
                str(item["node"]) for item in candidates if item.get("node") not in (None, "")
            }
            blocked_reason: str | None = None
            if requested_node is not None:
                matching = [
                    item for item in candidates if item.get("node") in (None, requested_node)
                ]
                live_item = matching[0] if matching else (candidates[0] if candidates else None)
                if candidates and not matching:
                    blocked_reason = "The requested node does not match the live resource node."
                operation_node = requested_node
            elif len(node_candidates) == 1:
                operation_node = next(iter(node_candidates))
                live_item = candidates[0] if candidates else None
            elif len(node_candidates) > 1:
                operation_node = None
                live_item = candidates[0]
                blocked_reason = (
                    "The live resource is present on multiple nodes; an exact node is required."
                )
            else:
                operation_node = None
                live_item = candidates[0] if candidates else None
            before_summary = (
                _plain(live_item.get("summary", {})) if isinstance(live_item, dict) else {}
            )
            after_summary = {
                key: value for key, value in _plain(desired_object.payload).items() if key != "node"
            }

            if action in {"delete", "destroy", "remove", "purge"}:
                planned_action = "delete"
            elif live_item is None:
                planned_action = "create"
            elif _payload_matches(after_summary, before_summary):
                planned_action = "noop"
            else:
                planned_action = "update"

            operations.append(
                ProviderOperation(
                    provider=self.provider,
                    kind=kind,
                    target_ref=target_ref,
                    action=planned_action,
                    node=operation_node,
                    supported=blocked_reason is None,
                    blocked_reason=blocked_reason,
                    before_summary=before_summary,
                    after_summary={} if planned_action == "delete" else after_summary,
                )
            )
        return operations

    async def plan(self, operations: list[ProviderOperation]) -> list[ProviderOperation]:
        node_names = _node_names(self._selected_session())
        planned: list[ProviderOperation] = []
        for operation in operations:
            item = operation.model_copy(deep=True)
            if not item.supported or item.action == "noop":
                planned.append(item)
                continue
            try:
                item.node = resolve_node(item, node_names)
                item.after_summary = validate_operation_payload(item)
            except CephCapabilityUnsupported:
                item.supported = False
                item.blocked_reason = (
                    "The Proxmox operation lacks an exact valid node or typed payload."
                )
            planned.append(item)
        return planned

    async def apply(
        self,
        operation: ProviderOperation,
        *,
        confirm_destructive: bool,
    ) -> dict[str, Any]:
        """Prepare and execute one operation for direct adapter callers."""

        dispatch = await self.prepare_apply(
            operation,
            confirm_destructive=confirm_destructive,
        )
        return await dispatch()

    async def prepare_apply(
        self,
        operation: ProviderOperation,
        *,
        confirm_destructive: bool,
    ) -> ProviderDispatch:
        """Finish all awaited gates before returning the mutation boundary."""

        operation_key = f"{operation.kind}:{operation.action}"
        if operation_kinds(True).get(operation_key) is not True:
            raise CephCapabilityUnsupported(
                "The Proxmox Ceph operation pair is not explicitly allowlisted."
            )
        if operation.action == "noop":

            async def dispatch_noop() -> dict[str, Any]:
                return {
                    "operation_id": operation.id,
                    "result": "noop",
                    "target_ref": operation.target_ref,
                }

            return dispatch_noop
        if not ceph_write_execution_enabled():
            raise CephWriteGateDenied(
                "ceph_write_execution_disabled",
                "Ceph writes require explicit operator and trusted actor gateway gates.",
            )
        if not self._writes_authorized or self._database_session is None:
            raise CephWriteGateDenied(
                "endpoint_write_gate_unavailable",
                "The selected endpoint is not authorized for Ceph writes.",
            )
        px = self._selected_session()
        client = _client_for(px)
        write = getattr(client, "write", None)
        if write is None:
            raise CephCapabilityUnsupported(
                "The installed proxmox-sdk does not expose Ceph write support; "
                "upgrade to a release that ships the CephWrite domain to enable "
                "Proxmox-backed Ceph writes."
            )
        if self._bound_session is None:
            raise CephWriteGateDenied(
                "endpoint_session_untagged",
                "Ceph writes require one privately bound local endpoint session.",
            )
        # Fetch remote membership before the final local authority check so a
        # slow provider read cannot widen the post-verification mutation window.
        node = resolve_node(operation, await _fresh_node_names(px))
        # This HMAC-backed comparison is the final provider-authority gate. The
        # engine performs a fresh durable-owner CAS after preparation returns
        # and before it invokes the returned mutation boundary.
        await self._bound_session.verify_fresh(self._database_session)

        async def dispatch() -> dict[str, Any]:
            result = await execute_operation(
                write,
                operation,
                node,
                confirm_destructive=confirm_destructive,
            )
            return {**result, "node": node}

        return dispatch

    async def wait_for_terminal(  # noqa: C901 - deadline/error boundary matrix
        self,
        node: str,
        upid: str,
        *,
        heartbeat: TaskHeartbeat | None = None,
    ) -> dict[str, str]:
        """Poll one submitted task using the same privately bound session."""

        px = self._selected_session()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._task_poll_timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return {"state": "outcome_unknown", "code": "provider_task_timeout"}
            if heartbeat is not None:
                await heartbeat()
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return {"state": "outcome_unknown", "code": "provider_task_timeout"}
            try:
                async with asyncio.timeout(remaining):
                    status = await get_node_task_status(px, node, upid)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                return {"state": "outcome_unknown", "code": "provider_task_timeout"}
            except Exception:  # noqa: BLE001 - transport details are secret-bearing
                return {
                    "state": "outcome_unknown",
                    "code": "provider_task_status_unavailable",
                }
            if loop.time() >= deadline:
                return {"state": "outcome_unknown", "code": "provider_task_timeout"}
            raw_state = (
                status.get("status")
                if isinstance(status, dict)
                else getattr(status, "status", None)
            )
            raw_exit = (
                status.get("exitstatus")
                if isinstance(status, dict)
                else getattr(status, "exitstatus", None)
            )
            if str(raw_state or "").casefold() == "stopped":
                if str(raw_exit or "").casefold() == "ok":
                    return {"state": "completed", "code": "provider_task_completed"}
                return {"state": "failed", "code": "provider_task_failed"}
            remaining = deadline - loop.time()
            if remaining <= 0:
                return {"state": "outcome_unknown", "code": "provider_task_timeout"}
            await asyncio.sleep(min(self._task_poll_interval, remaining))

    def declares_synchronous_success(
        self,
        operation: ProviderOperation,
        result: dict[str, Any],
    ) -> bool:
        """Accept only the exact synchronous result shape proven by the SDK."""

        operation_key = f"{operation.kind}:{operation.action}"
        has_task_reference = any(
            result.get(key) for key in ("upid", "provider_task_ref", "provider_task_refs")
        )
        return bool(
            operation_key in SYNCHRONOUS_OPERATION_KINDS
            and result.get("result") == "completed"
            and result.get("completion_mode") == "synchronous"
            and bool(operation.node)
            and result.get("node") == operation.node
            and not has_task_reference
        )

    async def reconcile(self, scope: dict[str, Any]) -> dict[str, Any]:
        live = await self.read_state(scope)
        return {
            "result": "read_only_reconcile",
            "summary": live.get("summary", {}),
            "errors": live.get("errors", []),
        }

    async def metrics(self, scope: dict[str, Any]) -> dict[str, Any]:
        live = await self.read_state(scope)
        summary = live.get("summary", {})
        return {
            "scope": scope,
            "clusters": summary.get("clusters", 0),
            "resources": summary.get("resources", 0),
            "errors": summary.get("errors", 0),
        }


__all__ = ["ProxmoxCephProviderAdapter"]

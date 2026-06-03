"""Proxmox-backed Ceph v2 provider adapter."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from proxbox_api.ceph.routes import _client_for, _node_names, _session_host, _session_name
from proxbox_api.ceph.v2_providers.base import CephCapabilityUnsupported, CephProviderAdapter
from proxbox_api.ceph.v2_providers.proxmox_writer import (
    cephwrite_importable,
    execute_operation,
    operation_kinds,
    resolve_node,
)
from proxbox_api.ceph.v2_schemas import (
    DesiredObject,
    DesiredStateBundle,
    ProviderCapabilities,
    ProviderOperation,
)
from proxbox_api.logger import logger


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


def _payload_matches(desired_payload: dict[str, Any], live_summary: dict[str, Any]) -> bool:
    if not desired_payload:
        return True
    for key, value in desired_payload.items():
        if key not in live_summary:
            return False
        if _plain(live_summary[key]) != _plain(value):
            return False
    return True


class ProxmoxCephProviderAdapter(CephProviderAdapter):
    """Read-only Ceph v2 adapter backed by resolved Proxmox sessions."""

    provider = "proxmox"

    def __init__(self, pxs: list[object] | None = None) -> None:
        self._pxs = list(pxs or [])

    async def capabilities(self) -> ProviderCapabilities:
        writes = cephwrite_importable()
        kinds: dict[str, bool] = {
            "noop": True,
            "pool:noop": True,
            "osd:noop": True,
            "filesystem:noop": True,
            "crush_rule:noop": True,
            "flag:noop": True,
        }
        kinds.update(operation_kinds(writes))
        if writes:
            notes = [
                "Proxmox Ceph v2 adapter reads, plans, and applies PVE-managed Ceph "
                "writes (pools, flags, OSD/MON/MGR/MDS lifecycle, CephFS) through the "
                "proxmox-sdk CephWrite helpers. Destructive operations require "
                "confirm_destructive."
            ]
        else:
            notes = [
                "Proxmox Ceph v2 adapter reads and plans from PVE Ceph state. Write "
                "execution is unavailable: the installed proxmox-sdk does not ship the "
                "CephWrite domain. Pin proxmox-sdk to a release that includes it to "
                "enable Proxmox-backed Ceph writes."
            ]
        return ProviderCapabilities(
            provider=self.provider,
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

        for px in self._pxs:
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
            except HTTPException as exc:
                message = str(exc.detail)
                cluster["errors"].append(message)
                errors.append(message)
                clusters.append(cluster)
                continue

            try:
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
            except Exception as exc:  # noqa: BLE001
                message = f"{type(exc).__name__}: {exc}"
                cluster["errors"].append(message)
                errors.append(message)
                logger.info("Ceph v2 state read failed for %s: %s", cluster["name"], exc)
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
        client: object,
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

    async def diff(
        self,
        desired: DesiredStateBundle,
        live: dict[str, Any],
    ) -> list[ProviderOperation]:
        live_index = {
            (
                _normalize_kind(str(item.get("kind", ""))),
                str(item.get("target_ref") or ""),
            ): item
            for item in live.get("resources", [])
            if isinstance(item, dict)
        }

        operations: list[ProviderOperation] = []
        for desired_object in desired.objects:
            kind = _normalize_kind(desired_object.kind)
            target_ref = _desired_target(desired_object)
            action = desired_object.action.strip().lower() or "ensure"
            live_item = live_index.get((kind, target_ref))
            before_summary = (
                _plain(live_item.get("summary", {})) if isinstance(live_item, dict) else {}
            )
            after_summary = _plain(desired_object.payload)

            if action in {"delete", "destroy", "remove", "purge"}:
                planned_action = "delete"
            elif live_item is None:
                planned_action = "create"
            elif _payload_matches(desired_object.payload, before_summary):
                planned_action = "noop"
            else:
                planned_action = "update"

            operations.append(
                ProviderOperation(
                    provider=self.provider,
                    kind=kind,
                    target_ref=target_ref,
                    action=planned_action,
                    before_summary=before_summary,
                    after_summary={} if planned_action == "delete" else after_summary,
                )
            )
        return operations

    async def plan(self, operations: list[ProviderOperation]) -> list[ProviderOperation]:
        return operations

    async def apply(
        self,
        operation: ProviderOperation,
        *,
        confirm_destructive: bool,
    ) -> dict[str, Any]:
        if operation.action == "noop":
            return {
                "operation_id": operation.id,
                "result": "noop",
                "target_ref": operation.target_ref,
            }
        if not self._pxs:
            raise CephCapabilityUnsupported(
                "No Proxmox session is available to apply Ceph write operations."
            )
        px = self._pxs[0]
        client = _client_for(px)
        write = getattr(client, "write", None)
        if write is None:
            raise CephCapabilityUnsupported(
                "The installed proxmox-sdk does not expose Ceph write support; "
                "upgrade to a release that ships the CephWrite domain to enable "
                "Proxmox-backed Ceph writes."
            )
        node = resolve_node(operation, _node_names(px))
        return await execute_operation(
            write, operation, node, confirm_destructive=confirm_destructive
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

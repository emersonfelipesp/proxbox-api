"""Direct Ceph Dashboard API provider adapter for Ceph v2 (#98).

Reads inventory and applies pool/RBD writes through the Ceph Manager Dashboard
REST API (via the ``proxmox-sdk`` Dashboard client), with **no Proxmox endpoint
required** — so it serves Proxmox-managed and external/standalone clusters
alike. Writes participate in the same plan/apply/operation-run flow as the
Proxmox provider.

Write capability is guarded by :func:`dashboard_sdk_importable`: an older
proxmox-sdk pin degrades to ``apply=False`` with a clear reason instead of
silently no-op'ing. The Dashboard endpoint config is resolved from
``scope["dashboard_endpoint"]`` (injected by the route, which has DB access) or
from a config passed to the constructor (tests / composition).
"""

from __future__ import annotations

from typing import Any

from proxbox_api.ceph.dashboard_client import (
    DashboardEndpointConfig,
    build_dashboard_client,
    dashboard_sdk_importable,
)
from proxbox_api.ceph.v2_providers.base import CephCapabilityUnsupported, CephProviderAdapter
from proxbox_api.ceph.v2_providers.dashboard_writer import (
    execute_dashboard_operation,
    operation_kinds,
)
from proxbox_api.ceph.v2_schemas import (
    DesiredObject,
    DesiredStateBundle,
    ProviderCapabilities,
    ProviderOperation,
)
from proxbox_api.logger import logger

_DESTRUCTIVE_ACTIONS = {"delete", "destroy", "remove", "purge"}


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_plain(v) for v in value]
    return value


def _normalize_kind(kind: str) -> str:
    cleaned = kind.strip().lower().replace("-", "_")
    aliases = {
        "pools": "pool",
        "osds": "osd",
        "rbd": "rbd_image",
        "rbd_images": "rbd_image",
        "rgw_buckets": "rgw_bucket",
        "buckets": "rgw_bucket",
    }
    return aliases.get(cleaned, cleaned)


def _desired_target(desired: DesiredObject) -> str:
    target = desired.target_ref or desired.name
    if target:
        return str(target)
    payload = desired.payload if isinstance(desired.payload, dict) else {}
    for key in ("name", "pool_name", "bucket"):
        if payload.get(key):
            return str(payload[key])
    return ""


def _payload_matches(desired_payload: dict[str, Any], live_summary: dict[str, Any]) -> bool:
    if not desired_payload:
        return True
    for key, value in desired_payload.items():
        if key not in live_summary or _plain(live_summary[key]) != _plain(value):
            return False
    return True


class DashboardCephProviderAdapter(CephProviderAdapter):
    """Ceph v2 adapter backed by the direct Ceph Dashboard REST API."""

    provider = "dashboard"

    def __init__(
        self,
        pxs: list[object] | None = None,  # noqa: ARG002 - registry-compatible signature
        *,
        endpoint: DashboardEndpointConfig | None = None,
        client_factory: Any = None,
    ) -> None:
        self._endpoint = endpoint
        # Injectable for tests: callable(config) -> async dashboard client.
        self._client_factory = client_factory

    # -- endpoint / client resolution --------------------------------------- #
    def _resolve_endpoint(self, scope: dict[str, Any]) -> DashboardEndpointConfig | None:
        candidate = self._endpoint or scope.get("dashboard_endpoint")
        return candidate if isinstance(candidate, DashboardEndpointConfig) else None

    def _make_client(self, config: DashboardEndpointConfig) -> Any:
        if self._client_factory is not None:
            return self._client_factory(config)
        return build_dashboard_client(config)

    # -- capabilities ------------------------------------------------------- #
    async def capabilities(self) -> ProviderCapabilities:
        importable = dashboard_sdk_importable()
        kinds = {"noop": True}
        kinds.update(operation_kinds(importable))
        if importable:
            notes = [
                "Direct Ceph Dashboard provider: reads inventory and applies pool / "
                "RBD writes through the Dashboard REST API. No Proxmox endpoint "
                "required. Destructive operations require confirm_destructive."
            ]
        else:
            notes = [
                "Ceph Dashboard provider is inactive: the installed proxmox-sdk does "
                "not ship proxmox_sdk.ceph.providers. Pin proxmox-sdk>=0.0.11 to "
                "enable Dashboard-backed reads and writes."
            ]
        return ProviderCapabilities(
            provider=self.provider,
            supported=True,
            read_state=importable,
            diff=importable,
            plan=importable,
            apply=importable,
            reconcile=importable,
            metrics=importable,
            operation_kinds=kinds,
            destructive_operations=importable,
            notes=notes,
        )

    # -- read ---------------------------------------------------------------- #
    async def read_state(self, scope: dict[str, Any]) -> dict[str, Any]:
        config = self._resolve_endpoint(scope)
        if config is None:
            return {
                "provider": self.provider,
                "clusters": [],
                "resources": [],
                "errors": ["no Ceph Dashboard endpoint configured"],
                "summary": {"clusters": 0, "resources": 0, "errors": 1},
            }
        resources: list[dict[str, Any]] = []
        errors: list[str] = []
        health: Any = None
        client = self._make_client(config)
        try:
            collectors = (
                ("pool", client.pools),
                ("osd", client.osds),
                ("host", client.hosts),
                ("filesystem", client.filesystems),
                ("rbd_image", client.rbd_images),
                ("rgw_bucket", client.rgw_buckets),
            )
            try:
                health = _plain(await client.health())
            except Exception as exc:  # noqa: BLE001
                errors.append(f"health: {type(exc).__name__}: {exc}")
            for kind, reader in collectors:
                try:
                    for item in await reader():
                        summary = _plain(item)
                        ref = _resource_ref(kind, summary)
                        if ref:
                            resources.append({"kind": kind, "target_ref": ref, "summary": summary})
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{kind}: {type(exc).__name__}: {exc}")
        finally:
            await _safe_close(client)

        cluster = {
            "provider": self.provider,
            "name": config.base_url,
            "host": config.base_url,
            "health": health,
            "errors": errors,
        }
        return {
            "provider": self.provider,
            "clusters": [cluster],
            "resources": resources,
            "errors": errors,
            "summary": {
                "clusters": 1,
                "resources": len(resources),
                "errors": len(errors),
                "health": (health or {}).get("status") if isinstance(health, dict) else None,
            },
        }

    async def diff(
        self,
        desired: DesiredStateBundle,
        live: dict[str, Any],
    ) -> list[ProviderOperation]:
        live_index = {
            (_normalize_kind(str(item.get("kind", ""))), str(item.get("target_ref") or "")): item
            for item in live.get("resources", [])
            if isinstance(item, dict)
        }
        operations: list[ProviderOperation] = []
        for obj in desired.objects:
            kind = _normalize_kind(obj.kind)
            target_ref = _desired_target(obj)
            action = obj.action.strip().lower() or "ensure"
            live_item = live_index.get((kind, target_ref))
            before = _plain(live_item.get("summary", {})) if isinstance(live_item, dict) else {}
            after = _plain(obj.payload)
            if action in _DESTRUCTIVE_ACTIONS:
                planned = "delete"
            elif live_item is None:
                planned = "create"
            elif _payload_matches(obj.payload, before):
                planned = "noop"
            else:
                planned = "update"
            operations.append(
                ProviderOperation(
                    provider=self.provider,
                    kind=kind,
                    target_ref=target_ref,
                    action=planned,
                    before_summary=before,
                    after_summary={} if planned == "delete" else after,
                )
            )
        return operations

    async def plan(self, operations: list[ProviderOperation]) -> list[ProviderOperation]:
        return operations

    # -- write --------------------------------------------------------------- #
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
        if not dashboard_sdk_importable():
            raise CephCapabilityUnsupported(
                "The installed proxmox-sdk does not ship the Ceph Dashboard client; "
                "pin proxmox-sdk>=0.0.11 to enable Dashboard-backed Ceph writes."
            )
        config = self._endpoint
        if config is None:
            raise CephCapabilityUnsupported(
                "No Ceph Dashboard endpoint is configured to apply the operation."
            )
        client = self._make_client(config)
        try:
            return await execute_dashboard_operation(
                client, operation, confirm_destructive=confirm_destructive
            )
        finally:
            await _safe_close(client)

    async def reconcile(self, scope: dict[str, Any]) -> dict[str, Any]:
        live = await self.read_state(scope)
        return {
            "result": "dashboard_reconcile",
            "summary": live.get("summary", {}),
            "errors": live.get("errors", []),
        }

    async def metrics(self, scope: dict[str, Any]) -> dict[str, Any]:
        if self._resolve_endpoint(scope) is None:
            return {}
        live = await self.read_state(scope)
        summary = live.get("summary", {})
        return {
            "scope": scope,
            "clusters": summary.get("clusters", 0),
            "resources": summary.get("resources", 0),
            "errors": summary.get("errors", 0),
            "health": summary.get("health"),
        }


def _resource_ref(kind: str, summary: dict[str, Any]) -> str | None:
    if not isinstance(summary, dict):
        return None
    candidates = {
        "pool": ("pool_name", "pool", "name"),
        "osd": ("osd", "id", "uuid"),
        "host": ("hostname", "addr"),
        "filesystem": ("name", "id"),
        "rbd_image": ("name", "id"),
        "rgw_bucket": ("bucket", "name"),
    }.get(kind, ("name", "id"))
    for key in candidates:
        value = summary.get(key)
        if value not in (None, ""):
            return str(value)
    return None


async def _safe_close(client: Any) -> None:
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception as exc:  # noqa: BLE001 - best-effort session cleanup
        logger.debug("Dashboard client close failed: %s", exc)


__all__ = ["DashboardCephProviderAdapter"]

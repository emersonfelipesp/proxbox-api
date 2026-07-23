"""External (non-Proxmox) Ceph cluster provider adapter for Ceph v2 (#97).

Composition adapter: binds a provider-neutral external Ceph cluster to its
configured sub-providers — Ceph Dashboard (inventory + pool/RBD writes),
Prometheus (metrics/health), and RGW Admin Ops (RGW inventory + writes) — with
**no Proxmox endpoint, node, storage, or task semantics**. Capability detection
is derived from which sub-providers are configured (and importable) plus the
Ceph version hint, so unsupported operations are reported, never faked.
"""

from __future__ import annotations

from typing import Any

from proxbox_api.ceph.dashboard_client import (
    DashboardEndpointConfig,
    dashboard_sdk_importable,
)
from proxbox_api.ceph.prometheus import PrometheusSourceConfig
from proxbox_api.ceph.rgw_client import (
    RGWAdminConfig,
    build_rgw_client,
    rgw_sdk_importable,
)
from proxbox_api.ceph.v2_providers.base import (
    CephCapabilityUnsupported,
    CephProviderAdapter,
    CephProviderBoundaryError,
    CephWriteGateDenied,
)
from proxbox_api.ceph.v2_providers.dashboard import DashboardCephProviderAdapter
from proxbox_api.ceph.v2_providers.dashboard_writer import operation_kinds as dashboard_kinds
from proxbox_api.ceph.v2_providers.prometheus import PrometheusCephProviderAdapter
from proxbox_api.ceph.v2_schemas import (
    DesiredStateBundle,
    ProviderCapabilities,
    ProviderOperation,
)
from proxbox_api.logger import logger

_RGW_KINDS = {"rgw_user", "rgw_bucket", "rgw_realm", "rgw_zone"}
_RGW_OPERATION_KINDS = {
    "rgw_user:create": True,
    "rgw_user:update": True,
    "rgw_user:suspend": True,
    "rgw_user:delete": True,
    "rgw_bucket:update": True,
    "rgw_bucket:delete": True,
}


class ExternalCephProviderAdapter(CephProviderAdapter):
    """Provider-neutral adapter for external (non-Proxmox) Ceph clusters."""

    provider = "external"

    def __init__(
        self,
        pxs: list[object] | None = None,  # noqa: ARG002 - registry-compatible signature
        *,
        dashboard: DashboardEndpointConfig | None = None,
        prometheus: PrometheusSourceConfig | None = None,
        rgw: RGWAdminConfig | None = None,
        ceph_version: str | None = None,
        dashboard_client_factory: Any = None,
        rgw_client_factory: Any = None,
    ) -> None:
        self._dashboard_cfg = dashboard
        self._prometheus_cfg = prometheus
        self._rgw_cfg = rgw
        self._ceph_version = ceph_version
        self._rgw_client_factory = rgw_client_factory
        self._dashboard = DashboardCephProviderAdapter(
            endpoint=dashboard, client_factory=dashboard_client_factory
        )
        self._prometheus = PrometheusCephProviderAdapter(source=prometheus) if prometheus else None

    # -- capability detection ----------------------------------------------- #
    def _dashboard_active(self) -> bool:
        return self._dashboard_cfg is not None and dashboard_sdk_importable()

    def _rgw_active(self) -> bool:
        return self._rgw_cfg is not None and (
            self._rgw_client_factory is not None or rgw_sdk_importable()
        )

    async def capabilities(self) -> ProviderCapabilities:
        dash = self._dashboard_active()
        rgw = self._rgw_active()
        metrics = self._prometheus_cfg is not None or dash
        kinds: dict[str, bool] = {}
        kinds.update(dashboard_kinds(False))
        for key in _RGW_OPERATION_KINDS:
            kinds[key] = False
        for kind in _RGW_KINDS:
            kinds[f"{kind}:noop"] = True
        configured = []
        if self._dashboard_cfg:
            configured.append("dashboard" + ("" if dash else " (needs proxmox-sdk>=0.0.11)"))
        if self._prometheus_cfg:
            configured.append("prometheus")
        if self._rgw_cfg:
            configured.append("rgw_admin" + ("" if rgw else " (needs proxmox-sdk>=0.0.11)"))
        notes = [
            "External (non-Proxmox) Ceph cluster. Configured providers: "
            + (", ".join(configured) if configured else "none")
            + (f". Ceph version hint: {self._ceph_version}." if self._ceph_version else ".")
        ]
        if not configured:
            notes.append("Configure a Dashboard, Prometheus, or RGW provider to enable operations.")
        notes.append(
            "External Ceph apply and destructive capabilities remain false until the "
            "cluster selector, provider credentials/revisions, and write authority are "
            "durably bound to the canonical plan."
        )
        return ProviderCapabilities(
            provider=self.provider,
            supported=True,
            read_state=dash or rgw,
            diff=dash or rgw,
            plan=dash or rgw,
            apply=False,
            reconcile=dash or rgw,
            metrics=metrics,
            operation_kinds=kinds,
            destructive_operations=False,
            notes=notes,
        )

    # -- read ---------------------------------------------------------------- #
    async def read_state(self, scope: dict[str, Any]) -> dict[str, Any]:
        resources: list[dict[str, Any]] = []
        errors: list[str] = []
        health: Any = None

        if self._dashboard_cfg is not None:
            dash_state = await self._dashboard.read_state(scope)
            resources.extend(dash_state.get("resources", []))
            errors.extend(dash_state.get("errors", []))
            health = (dash_state.get("summary") or {}).get("health")

        if self._rgw_cfg is not None:
            await self._read_rgw(resources, errors)

        if self._dashboard_cfg is None and self._rgw_cfg is None:
            errors.append("no external Ceph providers configured")

        return {
            "provider": self.provider,
            "clusters": [
                {
                    "provider": self.provider,
                    "name": self._cluster_name(),
                    "ceph_version": self._ceph_version,
                    "errors": errors,
                }
            ],
            "resources": resources,
            "errors": errors,
            "summary": {
                "clusters": 1,
                "resources": len(resources),
                "errors": len(errors),
                "health": health,
            },
        }

    async def _read_rgw(self, resources: list[dict[str, Any]], errors: list[str]) -> None:
        if not self._rgw_active():
            errors.append("rgw_admin: proxmox-sdk>=0.0.11 required")
            return
        client = self._make_rgw_client()
        try:
            try:
                for user in await client.list_users():
                    summary = _plain(user)
                    ref = summary.get("user_id") or summary.get("uid")
                    if ref:
                        resources.append(
                            {"kind": "rgw_user", "target_ref": str(ref), "summary": summary}
                        )
            except Exception:  # noqa: BLE001 - raw diagnostics are secret-bearing
                errors.append(_safe_diagnostic("rgw_user"))
            try:
                for bucket in await client.list_buckets():
                    resources.append(
                        {
                            "kind": "rgw_bucket",
                            "target_ref": str(bucket),
                            "summary": {"bucket": bucket},
                        }
                    )
            except Exception:  # noqa: BLE001 - raw diagnostics are secret-bearing
                errors.append(_safe_diagnostic("rgw_bucket"))
        finally:
            await _safe_close(client)

    async def diff(
        self,
        desired: DesiredStateBundle,
        live: dict[str, Any],
    ) -> list[ProviderOperation]:
        # Reuse the Dashboard diff (provider-neutral kind/target comparison),
        # then stamp the operations with this provider name.
        operations = await self._dashboard.diff(desired, live)
        for operation in operations:
            operation.provider = self.provider
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
        kind = operation.kind
        action = operation.action
        op_key = f"{kind}:{action}"
        known_dashboard_operation = dashboard_kinds(True).get(op_key) is True
        known_rgw_operation = _RGW_OPERATION_KINDS.get(op_key) is True or (
            action == "noop" and kind in _RGW_KINDS
        )
        if not known_dashboard_operation and not known_rgw_operation:
            raise CephCapabilityUnsupported(
                "External Ceph provider rejected an unsupported operation."
            )
        if operation.action == "noop":
            return {
                "operation_id": operation.id,
                "result": "noop",
                "target_ref": operation.target_ref,
            }
        raise CephWriteGateDenied(
            "durable_provider_write_gate_unavailable",
            "External Ceph writes require durable selector and write authority binding.",
        )

    def declares_synchronous_success(
        self,
        operation: ProviderOperation,  # noqa: ARG002
        result: dict[str, Any],
    ) -> bool:
        """External writes complete synchronously only on an explicit applied result."""

        return result.get("result") == "applied" and not (
            result.get("provider_task_ref") or result.get("upid")
        )

    async def _apply_rgw(
        self, operation: ProviderOperation, *, confirm_destructive: bool
    ) -> dict[str, Any]:
        if not self._rgw_active():
            raise CephCapabilityUnsupported(
                "External cluster has no active RGW Admin Ops provider (configure RGW "
                "credentials and pin proxmox-sdk>=0.0.11)."
            )
        payload = operation.after_summary if isinstance(operation.after_summary, dict) else {}
        target = operation.target_ref or ""
        client = self._make_rgw_client()
        try:
            raw = await _dispatch_rgw(
                client, operation.kind, operation.action, target, payload, confirm_destructive
            )
        finally:
            await _safe_close(client)
        return {
            "operation_id": operation.id,
            "result": "applied",
            "target_ref": operation.target_ref,
            "action": operation.action,
            "kind": operation.kind,
            "provider_summary": _plain(raw) if raw is not None else None,
        }

    async def reconcile(self, scope: dict[str, Any]) -> dict[str, Any]:
        live = await self.read_state(scope)
        return {
            "result": "external_reconcile",
            "summary": live.get("summary", {}),
            "errors": live.get("errors", []),
        }

    async def metrics(self, scope: dict[str, Any]) -> dict[str, Any]:
        if self._prometheus is not None:
            return await self._prometheus.metrics(scope)
        if self._dashboard_cfg is not None:
            return await self._dashboard.metrics(scope)
        return {}

    # -- helpers ------------------------------------------------------------- #
    def _make_rgw_client(self) -> Any:
        assert self._rgw_cfg is not None
        if self._rgw_client_factory is not None:
            return self._rgw_client_factory(self._rgw_cfg)
        return build_rgw_client(self._rgw_cfg)

    def _cluster_name(self) -> str:
        return "configured-external-ceph"


async def _dispatch_rgw(  # noqa: C901, PLR0911, PLR0912 - explicit kind/action table
    client: Any,
    kind: str,
    action: str,
    target: str,
    payload: dict[str, Any],
    confirm: bool,
) -> Any:
    if kind == "rgw_user":
        if action in ("create", "ensure"):
            display = payload.get("display_name") or payload.get("display-name") or target
            return await client.create_user(target, display_name=display)
        if action == "update":
            return await client.modify_user(target, **_rgw_user_fields(payload))
        if action == "suspend":
            return await client.set_user_suspended(target, suspended=True)
        if action == "delete":
            if not confirm:
                raise ValueError("rgw_user delete is destructive; confirm_destructive required.")
            return await client.remove_user(target, confirm_destroy=True)
    elif kind == "rgw_bucket":
        if action == "update":
            owner = payload.get("owner") or payload.get("uid")
            if owner:
                return await client.link_bucket(bucket=target, uid=str(owner))
            return None
        if action == "delete":
            if not confirm:
                raise ValueError("rgw_bucket delete is destructive; confirm_destructive required.")
            return await client.remove_bucket(target, confirm_destroy=True)
    raise CephCapabilityUnsupported(f"RGW Admin Ops adapter does not support {kind}:{action}.")


def _rgw_user_fields(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"display_name", "email", "max_buckets"}
    return {k: v for k, v in payload.items() if k in allowed and v is not None}


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_plain(v) for v in value]
    return value


async def _safe_close(client: Any) -> None:
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # noqa: BLE001 - best-effort cleanup
        logger.debug("RGW client close failed")


def _safe_diagnostic(kind: str) -> str:
    failure = CephProviderBoundaryError(
        "provider_read_unavailable",
        "Ceph provider data could not be read safely.",
    )
    return f"{kind}: {failure.reason} correlation_id={failure.correlation_id}"


__all__ = ["ExternalCephProviderAdapter"]

"""Ceph v2 provider adapter registry."""

from __future__ import annotations

from proxbox_api.ceph.v2_providers.base import (
    CephProviderAdapter,
    RBDCephProviderAdapter,
    RGWAdminCephProviderAdapter,
)


def _normalize_provider(provider: str | None) -> str:
    return (provider or "proxmox").strip().lower().replace("-", "_")


_PROVIDER_NAMES = frozenset({"proxmox", "dashboard", "rgw_admin", "rbd", "prometheus", "external"})


def provider_names() -> list[str]:
    return sorted(_PROVIDER_NAMES)


def adapter_for_provider(
    provider: str | None, pxs: list[object] | None = None
) -> CephProviderAdapter:
    name = _normalize_provider(provider)
    # Keep provider modules lazy. ``endpoint_binding`` imports ``base`` while
    # defining BoundProxmoxSession; eager-importing the Proxmox adapter here
    # would import that partially initialized module and disable all v2 routes.
    if name == "proxmox":
        from proxbox_api.ceph.v2_providers.proxmox import ProxmoxCephProviderAdapter

        return ProxmoxCephProviderAdapter(pxs)
    if name == "dashboard":
        from proxbox_api.ceph.v2_providers.dashboard import DashboardCephProviderAdapter

        return DashboardCephProviderAdapter(pxs)
    if name == "rgw_admin":
        return RGWAdminCephProviderAdapter(pxs)
    if name == "rbd":
        return RBDCephProviderAdapter(pxs)
    if name == "prometheus":
        from proxbox_api.ceph.v2_providers.prometheus import PrometheusCephProviderAdapter

        return PrometheusCephProviderAdapter(pxs)

    from proxbox_api.ceph.v2_providers.external import ExternalCephProviderAdapter

    return ExternalCephProviderAdapter(pxs)


__all__ = [
    "adapter_for_provider",
    "provider_names",
]

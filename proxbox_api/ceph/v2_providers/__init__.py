"""Ceph v2 provider adapter registry."""

from __future__ import annotations

from collections.abc import Callable

from proxbox_api.ceph.v2_providers.base import (
    CephProviderAdapter,
    DashboardCephProviderAdapter,
    ExternalCephProviderAdapter,
    PrometheusCephProviderAdapter,
    RBDCephProviderAdapter,
    RGWAdminCephProviderAdapter,
)
from proxbox_api.ceph.v2_providers.proxmox import ProxmoxCephProviderAdapter

ProviderFactory = Callable[[list[object] | None], CephProviderAdapter]


def _normalize_provider(provider: str | None) -> str:
    return (provider or "proxmox").strip().lower().replace("-", "_")


_REGISTRY: dict[str, ProviderFactory] = {
    "proxmox": ProxmoxCephProviderAdapter,
    "dashboard": DashboardCephProviderAdapter,
    "rgw_admin": RGWAdminCephProviderAdapter,
    "rbd": RBDCephProviderAdapter,
    "prometheus": PrometheusCephProviderAdapter,
    "external": ExternalCephProviderAdapter,
}


def provider_names() -> list[str]:
    return sorted(_REGISTRY)


def adapter_for_provider(
    provider: str | None, pxs: list[object] | None = None
) -> CephProviderAdapter:
    name = _normalize_provider(provider)
    factory = _REGISTRY.get(name, ExternalCephProviderAdapter)
    return factory(pxs)


__all__ = [
    "adapter_for_provider",
    "provider_names",
]

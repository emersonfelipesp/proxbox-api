"""Ceph v2 provider adapter interface and unsupported-provider stubs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from proxbox_api.ceph.v2_schemas import (
    DesiredStateBundle,
    ProviderCapabilities,
    ProviderOperation,
)


class CephCapabilityUnsupported(RuntimeError):
    """Raised when a Ceph v2 provider or operation is not implemented yet."""


class CephProviderAdapter(ABC):
    """Provider adapter contract for the Ceph v2 plan/apply engine."""

    provider: str

    @abstractmethod
    async def capabilities(self) -> ProviderCapabilities:
        """Return the adapter's supported Ceph v2 capabilities."""

    @abstractmethod
    async def read_state(self, scope: dict[str, Any]) -> dict[str, Any]:
        """Read current provider state for the requested scope."""

    @abstractmethod
    async def diff(
        self,
        desired: DesiredStateBundle,
        live: dict[str, Any],
    ) -> list[ProviderOperation]:
        """Compare desired and live state and return raw provider operations."""

    @abstractmethod
    async def plan(self, operations: list[ProviderOperation]) -> list[ProviderOperation]:
        """Normalize raw operations into provider-ready plan operations."""

    @abstractmethod
    async def apply(
        self,
        operation: ProviderOperation,
        *,
        confirm_destructive: bool,
    ) -> dict[str, Any]:
        """Apply one provider operation."""

    @abstractmethod
    async def reconcile(self, scope: dict[str, Any]) -> dict[str, Any]:
        """Run provider reconciliation for the requested scope."""

    @abstractmethod
    async def metrics(self, scope: dict[str, Any]) -> dict[str, Any]:
        """Return provider metrics for the requested scope."""


class UnsupportedCephProviderAdapter(CephProviderAdapter):
    """Stub adapter for provider integrations tracked by follow-up issues."""

    provider = "unsupported"
    followup = "follow-up issue"

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.provider,
            supported=False,
            notes=[f"{self.provider} provider is a stub pending {self.followup}."],
        )

    def _unsupported(self) -> CephCapabilityUnsupported:
        return CephCapabilityUnsupported(
            f"Ceph v2 provider {self.provider!r} is not implemented yet; pending {self.followup}."
        )

    async def read_state(self, scope: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        raise self._unsupported()

    async def diff(
        self,
        desired: DesiredStateBundle,  # noqa: ARG002
        live: dict[str, Any],  # noqa: ARG002
    ) -> list[ProviderOperation]:
        raise self._unsupported()

    async def plan(self, operations: list[ProviderOperation]) -> list[ProviderOperation]:  # noqa: ARG002
        raise self._unsupported()

    async def apply(
        self,
        operation: ProviderOperation,  # noqa: ARG002
        *,
        confirm_destructive: bool,  # noqa: ARG002
    ) -> dict[str, Any]:
        raise self._unsupported()

    async def reconcile(self, scope: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        raise self._unsupported()

    async def metrics(self, scope: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        raise self._unsupported()


class DashboardCephProviderAdapter(UnsupportedCephProviderAdapter):
    provider = "dashboard"
    followup = "#98"


class RGWAdminCephProviderAdapter(UnsupportedCephProviderAdapter):
    provider = "rgw_admin"
    followup = "#435 / proxmox-sdk#12"


class RBDCephProviderAdapter(UnsupportedCephProviderAdapter):
    provider = "rbd"
    followup = "#436 / proxmox-sdk#12"


class ExternalCephProviderAdapter(UnsupportedCephProviderAdapter):
    provider = "external"
    followup = "external-provider implementation"


__all__ = [
    "CephCapabilityUnsupported",
    "CephProviderAdapter",
    "DashboardCephProviderAdapter",
    "ExternalCephProviderAdapter",
    "RBDCephProviderAdapter",
    "RGWAdminCephProviderAdapter",
    "UnsupportedCephProviderAdapter",
]

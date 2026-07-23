"""Ceph v2 provider adapter interface and unsupported-provider stubs."""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from proxbox_api.ceph.v2_schemas import (
    DesiredStateBundle,
    ProviderCapabilities,
    ProviderOperation,
)

CEPH_WRITE_EXECUTION_ENV = "PROXBOX_ENABLE_CEPH_V2_WRITES"
CEPH_TRUSTED_ACTOR_GATEWAY_ENV = "PROXBOX_CEPH_TRUSTED_ACTOR_GATEWAY"
TaskHeartbeat = Callable[[], Awaitable[None]]


def _enabled_env(name: str) -> bool:
    return os.getenv(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def ceph_write_execution_enabled() -> bool:
    """Require operator opt-in plus a deployed actor-authentication gateway."""

    return _enabled_env(CEPH_WRITE_EXECUTION_ENV) and _enabled_env(CEPH_TRUSTED_ACTOR_GATEWAY_ENV)


class CephCapabilityUnsupported(RuntimeError):
    """Raised when a Ceph v2 provider or operation is not implemented yet."""


class CephWriteGateDenied(RuntimeError):
    """A fresh endpoint authorization check denied a provider mutation."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class CephProviderBoundaryError(RuntimeError):
    """Secret-free provider failure safe for API, audit, SSE, and logs."""

    def __init__(
        self,
        reason: str,
        detail: str,
        *,
        correlation_id: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.correlation_id = correlation_id or uuid4().hex


class CephProviderAdapter(ABC):
    """Provider adapter contract for the Ceph v2 plan/apply engine."""

    provider: str
    supports_task_heartbeat = False

    @property
    def database_session_lock(self) -> asyncio.Lock | None:
        """Optional lock serializing adapter and engine use of one DB session."""

        return None

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

    async def wait_for_terminal(
        self,
        node: str,  # noqa: ARG002
        upid: str,  # noqa: ARG002
        *,
        heartbeat: TaskHeartbeat | None = None,
    ) -> dict[str, str]:
        """Resolve a submitted provider task without assuming it completed."""

        if heartbeat is not None:
            await heartbeat()
        return {
            "state": "outcome_unknown",
            "code": "provider_task_status_unsupported",
        }

    def declares_synchronous_success(
        self,
        operation: ProviderOperation,  # noqa: ARG002
        result: dict[str, Any],  # noqa: ARG002
    ) -> bool:
        """Explicitly declare that a no-task result is known complete.

        The default is deliberately false. Providers whose mutation API is
        synchronous must override this method; absence of a task reference is
        never inferred to mean success.
        """

        return False

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


class RGWAdminCephProviderAdapter(UnsupportedCephProviderAdapter):
    provider = "rgw_admin"
    followup = "#435 / proxmox-sdk#12"


class RBDCephProviderAdapter(UnsupportedCephProviderAdapter):
    provider = "rbd"
    followup = "#436 / proxmox-sdk#12"


__all__ = [
    "CephCapabilityUnsupported",
    "CephProviderAdapter",
    "CephProviderBoundaryError",
    "CephWriteGateDenied",
    "CEPH_TRUSTED_ACTOR_GATEWAY_ENV",
    "CEPH_WRITE_EXECUTION_ENV",
    "RBDCephProviderAdapter",
    "RGWAdminCephProviderAdapter",
    "TaskHeartbeat",
    "UnsupportedCephProviderAdapter",
    "ceph_write_execution_enabled",
]

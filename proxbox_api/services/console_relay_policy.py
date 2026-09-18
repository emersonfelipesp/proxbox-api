"""Endpoint-policy seam for standalone Proxmox browser-console relay access.

The process-pinned RPC-only boundary is enforced before these routes by
``services.interactive_policy``. This module retains the separate endpoint-row
check at both ticket creation and consumption so endpoint-specific policy can
fail closed without changing either transport contract.
"""

from __future__ import annotations

from typing import Literal

from proxbox_api.database import ProxmoxEndpoint

ConsoleRelayPolicyStage = Literal["create", "consume"]


class ConsoleRelayPolicyDenied(RuntimeError):
    """The endpoint policy does not permit a browser console relay."""


def require_console_relay_endpoint_enabled(endpoint: ProxmoxEndpoint) -> None:
    """Reject disabled endpoints only on the standalone browser surface."""

    if not endpoint.enabled:
        raise ConsoleRelayPolicyDenied


def require_console_relay_policy(
    endpoint: ProxmoxEndpoint,
    *,
    stage: ConsoleRelayPolicyStage,
) -> None:
    """Apply the current endpoint policy at a relay trust boundary.

    This endpoint-specific seam remains a no-op. Keep ``endpoint`` and ``stage``
    in this signature: future policy must evaluate the current database row at
    both boundaries, including revocation between creation and consumption.
    """

    del endpoint, stage

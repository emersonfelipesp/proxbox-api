"""Policy seam for standalone Proxmox browser-console relay access.

Issue 395 may add an RPC-only endpoint policy. This module deliberately does
not implement or activate that policy; it provides one narrow check called at
both ticket creation and consumption so that future policy can fail closed
without changing either transport contract.
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

    This is intentionally a no-op until the separately reviewed RPC-only
    policy lands. Keep ``endpoint`` and ``stage`` in this signature: the future
    implementation must evaluate the current database row at both boundaries,
    including revocation between ticket creation and consumption.
    """

    del endpoint, stage

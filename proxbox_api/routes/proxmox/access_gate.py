"""SSH-transport access gate for Proxmox endpoints.

This is the enforcement point for the per-endpoint *access method* axis
(``ProxmoxEndpoint.access_methods``), which is orthogonal to the read/write
trust axis (``allow_writes`` / ``proxmox_actions._gate``):

- ``access_methods == "api"``  → Read+Write over the Proxmox HTTP API only.
- ``access_methods == "api_ssh"`` → Read+Write over the API **plus** SSH.

API is always the mandatory baseline; SSH-only is unrepresentable. Every code
path that actually initiates an SSH connection to a Proxmox endpoint (the
browser SSH terminal, the Cloud Image Build Pipeline remote execution) must run
through :func:`require_ssh_access` / :func:`gate_ssh_access` so SSH is refused
unless the endpoint opted into it.

Kept dependency-light (only FastAPI + the SQLModel row) to avoid importing the
heavy ``proxmox_actions`` module graph into the SSH terminal route.
"""

from __future__ import annotations

from fastapi import HTTPException, status

from proxbox_api.database import ProxmoxEndpoint
from proxbox_api.utils.async_compat import maybe_await as _maybe_await

SSH_ACCESS_DISABLED_REASON = "ssh_not_enabled_for_endpoint"


def require_ssh_access(endpoint: ProxmoxEndpoint) -> None:
    """Raise 403 when ``endpoint`` does not permit the SSH transport.

    SSH is allowed only when ``access_methods == "api_ssh"``. This never grants
    writes — it only governs whether the SSH transport may be used at all.
    """
    if not endpoint.ssh_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "reason": SSH_ACCESS_DISABLED_REASON,
                "detail": (
                    "SSH access is disabled on this endpoint. Set "
                    "access_methods to 'api_ssh' (API + SSH) on the endpoint to "
                    "permit SSH; SSH cannot be enabled without API."
                ),
                "endpoint_id": endpoint.id,
            },
        )


async def gate_ssh_access(session: object, endpoint_id: int | None) -> ProxmoxEndpoint:
    """Resolve ``endpoint_id`` and enforce SSH-transport access.

    ``session`` is a proxbox SQLModel session (sync or async); resolution goes
    through :func:`maybe_await` so both flavours work.
    """
    if endpoint_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "reason": "endpoint_id_required",
                "detail": "SSH access requires an explicit endpoint_id.",
            },
        )

    endpoint = await _maybe_await(session.get(ProxmoxEndpoint, endpoint_id))  # type: ignore[attr-defined]
    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "reason": "endpoint_not_found",
                "detail": f"No ProxmoxEndpoint with id={endpoint_id}.",
            },
        )

    require_ssh_access(endpoint)
    return endpoint

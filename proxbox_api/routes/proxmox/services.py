"""Read-only Proxmox systemd service-monitoring route.

Exposes ``GET /proxmox/services/systemd`` -- the SSH executor for the
agentless, pull-based Proxmox service-monitoring feature
(``emersonfelipesp/netbox-proxbox#180``). It is called by nms-backend's
``@rpc_handler("os.linux_proxmox.show_systemctl_services")``, itself
dispatched by the netbox-rpc procedure of the same name; it is not meant to be
called directly by end users.

Pulls systemd unit state over SSH using the endpoint's *own* registered SSH
credential (the same credential the browser SSH terminal serves), resolved
via the existing ``_fetch_endpoint_credential`` helper in
``proxbox_api.services.ssh_terminal``. ``endpoint_id`` here is the
**netbox-proxbox plugin's** ``ProxmoxEndpoint`` id -- the same id space the
browser SSH terminal uses -- and is *not* proxbox-api's own SQLite endpoint id
used by ``routes/cloud/qemu_templates.py`` or ``routes/proxmox/access_gate.py``.

Auth starts with the global ``X-Proxbox-API-Key`` middleware
(``APIKeyAuthMiddleware``), then this route reads the NetBox-side
``ProxmoxEndpoint`` detail record and refuses to fetch SSH credentials unless
service monitoring is enabled and the endpoint is eligible under the
netbox-proxbox gate (enabled endpoint, ``allow_writes``, ``api_ssh`` transport,
complete endpoint SSH credentials, and netbox-rpc enabled when exposed by the
plugin).
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from netbox_sdk.config import authorization_header_value

from proxbox_api.dependencies import NetBoxSessionDep
from proxbox_api.schemas.proxmox_services import (
    ProxmoxServiceError,
    ProxmoxServiceRecord,
    ProxmoxServicesResponse,
)
from proxbox_api.services.proxmox_services import (
    UnitValidationError,
    build_systemctl_show_command,
    parse_requested_units,
    parse_systemctl_show_output,
)
from proxbox_api.services.ssh_terminal import (
    SSHCommandError,
    SSHCommandTimeoutError,
    TerminalCredentialError,
    _fetch_endpoint_credential,
    _urlopen_kwargs,
    run_endpoint_command,
)

router = APIRouter()

_SSH_COMMAND_TIMEOUT_SECONDS = 10.0
_NETBOX_ENDPOINT_FETCH_TIMEOUT_SECONDS = 10.0
_COMMAND_ERROR_DETAIL_MAX_CHARS = 500


class ServiceMonitoringEndpointStateError(Exception):
    """Raised when NetBox endpoint service-monitoring state cannot be read."""


def _endpoint_state_url(base_url: str, endpoint_id: int) -> str:
    return f"{base_url.rstrip('/')}/api/plugins/proxbox/endpoints/proxmox/{int(endpoint_id)}/"


def _fetch_endpoint_service_monitoring_state(
    netbox_session: Any,
    endpoint_id: int,
    *,
    timeout: float = _NETBOX_ENDPOINT_FETCH_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Read the NetBox-side ProxmoxEndpoint fields used as the auth boundary."""
    config = netbox_session.client.config
    base_url = (config.base_url or "").rstrip("/")
    if not base_url:
        raise ServiceMonitoringEndpointStateError("NetBox base_url is not configured")

    auth = authorization_header_value(config)
    if not auth:
        raise ServiceMonitoringEndpointStateError("NetBox auth header could not be built")

    url = _endpoint_state_url(base_url, endpoint_id)
    req = urllib.request.Request(
        url,
        headers={"Authorization": auth, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, **_urlopen_kwargs(config, url, timeout)) as resp:
            if resp.status != 200:
                raise ServiceMonitoringEndpointStateError(
                    f"Endpoint state fetch returned HTTP {resp.status}"
                )
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise ServiceMonitoringEndpointStateError(
            f"Endpoint state fetch failed: HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ServiceMonitoringEndpointStateError(
            f"Endpoint state fetch failed: {exc.reason!s}"
        ) from exc

    try:
        payload = json.loads(body.decode())
    except json.JSONDecodeError as exc:
        raise ServiceMonitoringEndpointStateError(
            f"Endpoint state fetch for {endpoint_id} returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ServiceMonitoringEndpointStateError(
            f"Endpoint state fetch for {endpoint_id} returned non-object payload"
        )
    return payload


def _endpoint_state_error_to_http_exception(
    endpoint_id: int, exc: ServiceMonitoringEndpointStateError
) -> HTTPException:
    cause = exc.__cause__
    if isinstance(cause, urllib.error.HTTPError):
        if cause.code == 404:
            return HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "reason": "proxmox_endpoint_not_found",
                    "detail": f"No NetBox ProxmoxEndpoint with id={endpoint_id}",
                },
            )
        if cause.code in {401, 403}:
            return HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "reason": "service_monitoring_endpoint_state_forbidden",
                    "detail": "NetBox refused access to the endpoint service-monitoring state.",
                },
            )
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "netbox_endpoint_state_fetch_failed", "detail": str(exc)},
        )
    if isinstance(cause, urllib.error.URLError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "netbox_unreachable", "detail": str(exc)},
        )
    if isinstance(cause, json.JSONDecodeError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "invalid_netbox_response", "detail": str(exc)},
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"reason": "endpoint_state_fetch_failed", "detail": str(exc)},
    )


def _endpoint_service_monitoring_units(
    endpoint_state: dict[str, Any],
) -> str | Sequence[str] | None:
    units = endpoint_state.get("service_monitoring_units")
    if units is None:
        return None
    if isinstance(units, str):
        return units
    if isinstance(units, Sequence):
        return units
    raise UnitValidationError("service_monitoring_units must be a list of strings")


def _require_service_monitoring_authorized(endpoint_state: dict[str, Any]) -> None:
    """Fail closed unless NetBox says this endpoint may run service monitoring."""
    if not bool(endpoint_state.get("enabled", True)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "reason": "service_monitoring_endpoint_disabled",
                "detail": "Disabled NetBox ProxmoxEndpoints are never contacted.",
            },
        )
    if not bool(endpoint_state.get("service_monitoring_enabled", False)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "reason": "service_monitoring_disabled",
                "detail": "Service monitoring is not enabled for this endpoint.",
            },
        )

    eligible = endpoint_state.get("service_monitoring_eligible")
    if eligible is True:
        return

    if eligible is None:
        eligible = (
            bool(endpoint_state.get("allow_writes", False))
            and str(endpoint_state.get("access_methods") or "") == "api_ssh"
            and bool(endpoint_state.get("has_ssh_terminal_credentials", False))
            and endpoint_state.get("effective_rpc_enabled", True) is not False
        )
        if eligible:
            return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "reason": "service_monitoring_ineligible",
            "detail": (
                "Service monitoring requires allow_writes, API + SSH access, "
                "complete endpoint SSH credentials, and netbox-rpc enabled for this endpoint."
            ),
        },
    )


def _bounded_text(value: str, *, limit: int = _COMMAND_ERROR_DETAIL_MAX_CHARS) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...[truncated]"


def _command_failed_error(completed: object) -> ProxmoxServiceError:
    stderr = _bounded_text(str(getattr(completed, "stderr", "") or ""))
    suffix = stderr or "no stderr"
    return ProxmoxServiceError(
        reason="command_failed",
        detail=(
            f"systemctl show exited with status {getattr(completed, 'exit_status', -1)}: {suffix}"
        ),
    )


def _credential_error_to_http_exception(
    endpoint_id: int, exc: TerminalCredentialError
) -> HTTPException:
    """Map a credential-resolution failure to its HTTP error contract.

    Inspects ``exc.__cause__`` (chained via ``raise ... from exc`` inside
    ``_fetch_endpoint_credential`` / ``_coerce_endpoint_credential``) instead
    of parsing the exception message, so each branch traces back to a
    specific, testable failure mode rather than a string match.
    """
    cause = exc.__cause__
    if isinstance(cause, urllib.error.HTTPError):
        if cause.code == 404:
            return HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "reason": "ssh_credential_not_found",
                    "detail": f"No SSH credential registered for endpoint {endpoint_id}",
                },
            )
        if cause.code == 403:
            return HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"reason": "ssh_not_enabled_for_endpoint", "detail": str(exc)},
            )
        if cause.code == 422:
            return HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"reason": "invalid_endpoint_ssh_config", "detail": str(exc)},
            )
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "netbox_credential_fetch_failed", "detail": str(exc)},
        )
    if isinstance(cause, urllib.error.URLError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "netbox_unreachable", "detail": str(exc)},
        )
    if isinstance(cause, json.JSONDecodeError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"reason": "invalid_netbox_response", "detail": str(exc)},
        )
    if isinstance(cause, (TypeError, ValueError)):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "incomplete_ssh_credential", "detail": str(exc)},
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"reason": "credential_fetch_failed", "detail": str(exc)},
    )


@router.get("/services/systemd", response_model=ProxmoxServicesResponse)
async def get_systemd_services(
    netbox_session: NetBoxSessionDep,
    endpoint_id: Annotated[int, Query(ge=1)],
    units: Annotated[list[str] | None, Query()] = None,
) -> ProxmoxServicesResponse:
    """Pull current systemd service state from a Proxmox endpoint over SSH.

    ``endpoint_id`` is the netbox-proxbox plugin's ``ProxmoxEndpoint`` id.
    ``units`` is an optional repeated and/or comma-separated unit list; omitted
    means the endpoint's NetBox ``service_monitoring_units`` value, falling
    back to the default Proxmox unit set when that value is empty.

    Returns HTTP 200 with ``reachable=False`` when the endpoint's SSH
    transport cannot be reached (connect timeout, refused, authentication
    failure) -- that is a legitimate monitoring result, not an error. Misuse
    (unknown endpoint, no/disabled SSH credential, malformed unit request) is
    surfaced as a 4xx.
    """
    try:
        requested_units = parse_requested_units(units) if units is not None else None
    except UnitValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"reason": "invalid_units", "detail": str(exc)},
        ) from exc

    try:
        endpoint_state = await asyncio.to_thread(
            _fetch_endpoint_service_monitoring_state,
            netbox_session,
            endpoint_id,
        )
    except ServiceMonitoringEndpointStateError as exc:
        raise _endpoint_state_error_to_http_exception(endpoint_id, exc) from exc

    _require_service_monitoring_authorized(endpoint_state)

    if requested_units is None:
        try:
            requested_units = parse_requested_units(
                _endpoint_service_monitoring_units(endpoint_state)
            )
        except UnitValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"reason": "invalid_units", "detail": str(exc)},
            ) from exc

    try:
        credential = await asyncio.to_thread(
            _fetch_endpoint_credential,
            netbox_session,
            endpoint_id,
            None,
        )
    except TerminalCredentialError as exc:
        raise _credential_error_to_http_exception(endpoint_id, exc) from exc

    command = build_systemctl_show_command(requested_units)
    collected_at = datetime.now(UTC)
    try:
        completed = await run_endpoint_command(
            credential, command, timeout=_SSH_COMMAND_TIMEOUT_SECONDS
        )
    except SSHCommandTimeoutError as exc:
        return ProxmoxServicesResponse(
            endpoint_id=endpoint_id,
            host=credential.host,
            collected_at=collected_at,
            reachable=True,
            services=[],
            error=ProxmoxServiceError(reason="command_timeout", detail=str(exc)),
        )
    except (TerminalCredentialError, SSHCommandError) as exc:
        return ProxmoxServicesResponse(
            endpoint_id=endpoint_id,
            host=credential.host,
            collected_at=collected_at,
            reachable=False,
            services=[],
            error=ProxmoxServiceError(reason="ssh_unreachable", detail=str(exc)),
        )

    if completed.exit_status != 0:
        return ProxmoxServicesResponse(
            endpoint_id=endpoint_id,
            host=credential.host,
            collected_at=collected_at,
            reachable=True,
            services=[],
            error=_command_failed_error(completed),
        )

    records = parse_systemctl_show_output(completed.stdout, requested_units)
    return ProxmoxServicesResponse(
        endpoint_id=endpoint_id,
        host=credential.host,
        collected_at=collected_at,
        reachable=True,
        services=[ProxmoxServiceRecord(**record) for record in records],
    )

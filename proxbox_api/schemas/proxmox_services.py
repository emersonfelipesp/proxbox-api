"""Response schemas for the read-only Proxmox systemd service-monitoring route.

Backs ``GET /proxmox/services/systemd`` (see
``proxbox_api/routes/proxmox/services.py``). Field names and shape mirror the
"Shared contract" published in the parent feature issue
(``emersonfelipesp/netbox-proxbox#180``) so netbox-rpc / nms-backend callers
and this schema never drift.
"""

from datetime import datetime

from pydantic import Field

from proxbox_api.schemas._base import ProxboxStrictModel


class ProxmoxServiceRecord(ProxboxStrictModel):
    """One systemd unit's parsed ``systemctl show`` state.

    Field names are the snake_case projection of the ``systemctl show -p``
    properties collected by
    ``proxbox_api.services.proxmox_services.parse_systemctl_show_output``:
    ``Id, LoadState, ActiveState, SubState, Result, MainPID, ExecMainCode,
    ExecMainStatus, NRestarts, ActiveEnterTimestamp, UnitFileState``.
    """

    unit: str
    id: str
    load_state: str
    active_state: str
    sub_state: str
    result: str
    main_pid: int
    exec_main_code: int
    exec_main_status: int
    n_restarts: int
    active_enter_timestamp: str
    unit_file_state: str


class ProxmoxServiceError(ProxboxStrictModel):
    """Structured reason a collection could not reach the Proxmox endpoint."""

    reason: str
    detail: str


class ProxmoxServicesResponse(ProxboxStrictModel):
    """Response envelope for ``GET /proxmox/services/systemd``.

    ``reachable=False`` is a legitimate monitoring result (the endpoint's SSH
    transport could not be reached) and is always returned as HTTP 200 with
    empty ``services`` and a populated ``error`` -- this is what lets the
    caller (netbox-rpc / netbox-proxbox heartbeat projection) tell "node down"
    apart from "call was misused". Misuse (unknown endpoint id, no SSH
    credential registered, SSH disabled for the endpoint, malformed unit
    request) is surfaced as a 4xx ``HTTPException`` instead of this schema.
    """

    endpoint_id: int
    host: str
    collected_at: datetime
    reachable: bool
    services: list[ProxmoxServiceRecord] = Field(default_factory=list)
    error: ProxmoxServiceError | None = None

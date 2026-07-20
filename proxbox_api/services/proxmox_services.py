"""Proxmox systemd service-monitoring: unit validation, command build, parsing.

Read-only support for ``GET /proxmox/services/systemd``
(``proxbox_api/routes/proxmox/services.py``). This module never opens a
network connection -- it only validates requested unit names, builds the
fixed-argv ``systemctl show`` command string, and parses the command's
captured stdout into typed records. The actual SSH execution lives in
``proxbox_api.services.ssh_terminal.run_endpoint_command``.

Contract source of truth: the "Shared contract" section of the parent feature
issue (``emersonfelipesp/netbox-proxbox#180``), which lists the collected
systemd properties, the default Proxmox unit set, and the unit
validation rule shared by every layer (proxbox-api, nms-backend, netbox-rpc).
"""

import re
import shlex
from collections.abc import Sequence

# The 11 Proxmox units this feature monitors by default. All are optional per
# node -- a unit not installed on a given host still returns a valid
# ``systemctl show`` block (``LoadState=not-found``), so its absence is a
# real, storable state rather than an error. Never assume every unit must be
# active/running (e.g. ``pve-ha-crm``/``pve-ha-lrm`` are commonly
# ``active/exited`` outside an HA-managed cluster).
PROXMOX_MONITORED_UNITS_DEFAULT: frozenset[str] = frozenset(
    {
        "pve-cluster.service",
        "corosync.service",
        "pvedaemon.service",
        "pveproxy.service",
        "pvestatd.service",
        "pve-firewall.service",
        "pvescheduler.service",
        "spiceproxy.service",
        "qmeventd.service",
        "pve-ha-lrm.service",
        "pve-ha-crm.service",
    }
)

_UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@:-]*$")
_MAX_UNIT_NAME_LENGTH = 100
_MAX_UNITS_PER_REQUEST = 32

# Systemd properties collected via ``systemctl show -p <prop>``, in the exact
# order the parent issue's shared contract specifies. Order matters here only
# for the argv build below -- parsing keys the parsed block off property name,
# not position.
SYSTEMCTL_SHOW_PROPERTIES: tuple[str, ...] = (
    "Id",
    "LoadState",
    "ActiveState",
    "SubState",
    "Result",
    "MainPID",
    "ExecMainCode",
    "ExecMainStatus",
    "NRestarts",
    "ActiveEnterTimestamp",
    "UnitFileState",
)

# systemctl property name -> ProxmoxServiceRecord snake_case field name.
_PROPERTY_TO_FIELD: dict[str, str] = {
    "Id": "id",
    "LoadState": "load_state",
    "ActiveState": "active_state",
    "SubState": "sub_state",
    "Result": "result",
    "MainPID": "main_pid",
    "ExecMainCode": "exec_main_code",
    "ExecMainStatus": "exec_main_status",
    "NRestarts": "n_restarts",
    "ActiveEnterTimestamp": "active_enter_timestamp",
    "UnitFileState": "unit_file_state",
}

# Fields coerced to int; everything else (including the systemd-native
# timestamp string) is kept as a raw string.
_INT_FIELDS = frozenset({"main_pid", "exec_main_code", "exec_main_status", "n_restarts"})


class UnitValidationError(ValueError):
    """Raised when a requested systemd unit name fails validation."""


def _validate_unit_name(unit: str) -> str:
    """Validate a single unit name against the shared contract rule.

    Every layer of this feature (proxbox-api, nms-backend, netbox-rpc)
    enforces the same rule: ``^[A-Za-z0-9_][A-Za-z0-9_.@:-]*$``, no ``..``,
    at most :data:`_MAX_UNIT_NAME_LENGTH` characters. The default Proxmox unit
    set is not a hard allowlist: operators may configure additional systemd
    units in netbox-proxbox and forward them here under the same validation
    contract.
    """
    if not isinstance(unit, str):
        raise UnitValidationError("Unit name must be a string")
    name = unit.strip()
    if not name:
        raise UnitValidationError("Unit name must not be empty")
    if len(name) > _MAX_UNIT_NAME_LENGTH:
        raise UnitValidationError(f"Unit name exceeds {_MAX_UNIT_NAME_LENGTH} characters: {name!r}")
    if ".." in name:
        raise UnitValidationError(f"Unit name must not contain '..': {name!r}")
    if not _UNIT_NAME_RE.match(name):
        raise UnitValidationError(f"Unit name has invalid characters: {name!r}")
    return name


def parse_requested_units(units: str | Sequence[str] | None) -> list[str]:
    """Parse and validate requested systemd units.

    An empty or ``None`` value returns the full default Proxmox unit
    set (sorted, for deterministic output). Otherwise ``units`` may be a
    comma-separated string or a sequence of strings; each entry is validated with
    :func:`_validate_unit_name` and the whole request is capped at
    :data:`_MAX_UNITS_PER_REQUEST` units.

    Raises:
        UnitValidationError: any requested unit is malformed, too long,
            contains ``..``, or more than :data:`_MAX_UNITS_PER_REQUEST`
            units were requested.
    """
    if units is None:
        return sorted(PROXMOX_MONITORED_UNITS_DEFAULT)

    raw_values: Sequence[str]
    if isinstance(units, str):
        raw_values = [units]
    else:
        raw_values = units

    requested: list[str] = []
    for value in raw_values:
        if not isinstance(value, str):
            raise UnitValidationError("Unit name must be a string")
        requested.extend(part for part in (piece.strip() for piece in value.split(",")) if part)

    if not requested:
        return sorted(PROXMOX_MONITORED_UNITS_DEFAULT)
    if len(requested) > _MAX_UNITS_PER_REQUEST:
        raise UnitValidationError(
            f"At most {_MAX_UNITS_PER_REQUEST} units may be requested per call"
        )
    return [_validate_unit_name(unit) for unit in requested]


def build_systemctl_show_command(units: Sequence[str]) -> str:
    """Build the fixed-argv ``systemctl show`` command for ``units``.

    Every token except the unit names is a hardcoded literal, never
    influenced by caller input. Callers must pass units that have already
    been validated by :func:`parse_requested_units` (regex + length cap); each
    unit name is additionally ``shlex.quote``d here as defense in depth, so no
    caller-controlled value can break out of its argument position on the
    remote shell an SSH exec channel invokes to run the command.

    No ``sudo`` -- ``systemctl show`` requires no privileges.
    """
    argv: list[str] = ["systemctl", "show", "--no-pager"]
    for prop in SYSTEMCTL_SHOW_PROPERTIES:
        argv.extend(["-p", prop])
    argv.append("--")
    argv.extend(shlex.quote(unit) for unit in units)
    return " ".join(argv)


def _split_blocks(raw_output: str) -> list[str]:
    """Split ``systemctl show`` output into one text block per queried unit.

    systemd separates each unit's ``KEY=VALUE`` property block with a blank
    line when more than one unit is queried. Leading/trailing blank lines
    (e.g. a trailing newline at EOF) never produce an empty block.
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in raw_output.splitlines():
        if line.strip() == "":
            if current:
                blocks.append("\n".join(current))
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _block_to_properties(block: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in block.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        properties[key.strip()] = value.strip()
    return properties


def _coerce_int(value: str | None, *, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _block_to_record(unit: str, block: str) -> dict[str, object]:
    properties = _block_to_properties(block)
    record: dict[str, object] = {"unit": unit}
    for prop_name, field_name in _PROPERTY_TO_FIELD.items():
        raw_value = properties.get(prop_name, "")
        record[field_name] = _coerce_int(raw_value) if field_name in _INT_FIELDS else raw_value
    return record


def parse_systemctl_show_output(raw_output: str, units: Sequence[str]) -> list[dict[str, object]]:
    """Parse ``systemctl show`` stdout into one record dict per requested unit.

    Blocks are paired with ``units`` **positionally**, in request order --
    not by the parsed ``Id=`` value -- because systemd echoes back a
    synthesized block (``LoadState=not-found``) for any syntactically valid
    unit name, including ones that are not installed, so a keyed lookup would
    not be any more reliable than trusting the request order systemd
    preserves.

    If the parsed block count does not match ``len(units)``, the shorter
    length wins and unmatched trailing units are silently dropped rather than
    raising: this is a read-only monitoring path, so a partial result beats a
    hard failure.

    Each returned dict matches
    ``proxbox_api.schemas.proxmox_services.ProxmoxServiceRecord`` field names
    and can be passed to it as ``ProxmoxServiceRecord(**record)``.
    """
    blocks = _split_blocks(raw_output)
    paired = zip(units, blocks, strict=False)
    return [_block_to_record(unit, block) for unit, block in paired]

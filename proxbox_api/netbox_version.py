"""Live NetBox version detection and capability gates.

`/api/status/` returns ``{"netbox-version": "4.6.0", ...}`` on every
supported NetBox release. This module fetches that value once per session,
caches it on the session object, and exposes a small comparison API. Sync
services use it to gate 4.6-only behaviours (for example, creating
``VirtualMachineType`` rows). Keep this module aligned with the rolling
3-version policy in CLAUDE.md.
"""

from __future__ import annotations

from proxbox_api.constants import (
    NETBOX_SCHEMA_FLOOR,
    NETBOX_SCHEMA_VERSION,
    SUPPORTED_NETBOX_MAJOR_MINOR,
)
from proxbox_api.logger import logger

_CACHE_ATTR = "_proxbox_netbox_version"
_SCHEMA_WARNED_ATTR = "_proxbox_netbox_schema_warned"


def parse_netbox_version(raw: str | None) -> tuple[int, int, int]:
    """Parse a NetBox version string into ``(major, minor, patch)``.

    Tolerates pre-release suffixes (``"4.6.0-beta2"``) and short forms
    (``"4.5"``). Returns ``(0, 0, 0)`` on empty/invalid input.
    """
    if not raw:
        return (0, 0, 0)
    head = str(raw).split("-", 1)[0].strip().removeprefix("v")
    parts = head.split(".")
    out: list[int] = []
    for piece in parts[:3]:
        try:
            out.append(int(piece))
        except ValueError:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return (out[0], out[1], out[2])


def _unwrap(nb: object) -> object:
    """Best-effort: return the underlying netbox-sdk Api regardless of facade depth."""
    try:
        from proxbox_api.netbox_rest import _unwrap_api  # local import to avoid cycle

        return _unwrap_api(nb)
    except Exception:
        return nb


async def detect_netbox_version(nb: object) -> tuple[int, int, int]:
    """Return the live NetBox version, caching the result on the api object.

    Falls back to ``(0, 0, 0)`` and logs a warning if the call fails so callers
    can treat unknown as "oldest" without raising.
    """
    api = _unwrap(nb)
    cached = getattr(api, _CACHE_ATTR, None)
    if cached is not None:
        return cached

    try:
        response = await api.client.request("GET", "/api/status/")  # type: ignore[attr-defined]
        payload = getattr(response, "data", None) or getattr(response, "json", None) or {}
        if callable(payload):
            payload = payload()
        if not isinstance(payload, dict):
            payload = {}
        raw_version = payload.get("netbox-version") or payload.get("netbox_version")
    except Exception as exc:
        logger.warning("Could not detect NetBox version via /api/status/: %s", exc)
        raw_version = None

    version = parse_netbox_version(raw_version)
    try:
        setattr(api, _CACHE_ATTR, version)
    except Exception:
        # Some Api facades may forbid attribute assignment; cache miss is fine.
        pass
    _maybe_warn_schema_mismatch(api, version, raw_version)
    return version


def _maybe_warn_schema_mismatch(
    api: object, version: tuple[int, int, int], raw_version: str | None
) -> None:
    """Log once per session when the live NetBox major.minor isn't bundled.

    Surface mismatches between the live NetBox release line and the schema
    configured for proxbox-api so operators can see when runtime capability
    gates are carrying compatibility across supported versions.
    """
    if version == (0, 0, 0):
        return
    if getattr(api, _SCHEMA_WARNED_ATTR, False):
        return
    live_mm = f"{version[0]}.{version[1]}"
    if live_mm not in SUPPORTED_NETBOX_MAJOR_MINOR:
        logger.warning(
            "Live NetBox %s is outside the supported window %s; using floor schema %s. "
            "Capability gates may misbehave.",
            raw_version or live_mm,
            ",".join(SUPPORTED_NETBOX_MAJOR_MINOR),
            NETBOX_SCHEMA_FLOOR,
        )
    elif live_mm != NETBOX_SCHEMA_VERSION:
        logger.info(
            "Live NetBox %s detected; configured SDK schema is %s. "
            "Sync uses runtime capability checks for version-gated behaviour.",
            raw_version or live_mm,
            NETBOX_SCHEMA_VERSION,
        )
    try:
        setattr(api, _SCHEMA_WARNED_ATTR, True)
    except Exception:
        pass


def is_at_least(version: tuple[int, int, int], major: int, minor: int, patch: int = 0) -> bool:
    """Return True when ``version`` is at or above ``(major, minor, patch)``."""
    return version >= (major, minor, patch)


def supports_virtual_machine_type(version: tuple[int, int, int]) -> bool:
    """Return True when the live NetBox exposes the 4.6 VirtualMachineType model."""
    return is_at_least(version, 4, 6)

"""Parse opt-in ``netbox-metadata`` JSON blocks embedded in Proxmox descriptions.

Operators may embed a fenced JSON block in any Proxmox object description to
override the NetBox foreign keys that proxbox-api would otherwise resolve from
the endpoint placement metadata or built-in mappings::

    ```netbox-metadata
    {
      "device": 2,
      "tenant": 13,
      "site": 4
    }
    ```

Values are NetBox primary-key integers. Keys are unrestricted on this side;
unknown keys are forwarded to NetBox so the API can reject them. The block is
opt-in (gated by the plugin's ``parse_description_metadata`` setting); when the
toggle is off, descriptions are ignored exactly as they were before this
feature shipped.

Issue: https://github.com/emersonfelipesp/netbox-proxbox/issues/366
"""

from __future__ import annotations

import json
import re

# Fence regex: matches one fenced block whose info string is ``netbox-metadata``
# (case-insensitive). The closing fence must sit on its own line. Multiple
# blocks in one description are tolerated; the last block wins, matching the
# original issue's ``duplicated keys -> last-wins`` rule.
_FENCE_RE = re.compile(
    r"^[ \t]*```[ \t]*netbox-metadata[ \t]*\r?\n(?P<body>.*?)\r?\n[ \t]*```[ \t]*$",
    re.IGNORECASE | re.DOTALL | re.MULTILINE,
)


def parse_netbox_metadata(text: str | None) -> dict[str, int]:
    """Extract ``netbox-metadata`` PK overrides from a Proxmox description.

    Returns ``{}`` for any non-conforming input: missing fence, malformed JSON,
    non-object JSON body, or non-integer/non-positive values. Each invalid
    value is silently dropped; the remaining valid keys are still returned so
    one bad entry does not break the whole block.

    On multiple ``netbox-metadata`` blocks in the same description, the last
    one wins (consistent with the original issue's duplicate-key rule).
    """
    if not text:
        return {}

    matches = list(_FENCE_RE.finditer(text))
    if not matches:
        return {}

    body = matches[-1].group("body").strip()
    if not body:
        return {}

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return {}

    if not isinstance(payload, dict):
        return {}

    result: dict[str, int] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        # bool is a subclass of int in Python; reject it explicitly so
        # ``"role": true`` does not silently become role=1.
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value <= 0:
            continue
        result[key] = value
    return result


def strip_netbox_metadata(text: str | None) -> str | None:
    """Return ``text`` with every ``netbox-metadata`` fenced block removed.

    The empty-string result is collapsed to ``None`` so callers can fall back
    to the historical ``"Synced from Proxmox node {node}"`` placeholder
    instead of writing a blank description to NetBox.
    """
    if not text:
        return text
    cleaned = _FENCE_RE.sub("", text).strip()
    return cleaned or None


def filter_metadata_by_overwrite_flags(
    metadata: dict[str, int],
    overwrite_flags: object | None,
    *,
    object_kind: str,
) -> tuple[dict[str, int], list[str]]:
    """Drop metadata keys whose matching overwrite flag is off.

    ``object_kind`` is the per-object prefix used to look up overwrite flags
    on the plugin side (e.g. ``"vm"`` -> ``overwrite_vm_role`` for key
    ``role``). Keys with no corresponding overwrite flag are kept
    unconditionally because the user explicitly opted into the
    ``parse_description_metadata`` toggle.

    Returns ``(applied, dropped)`` where ``dropped`` is the alphabetically
    sorted list of keys that were filtered out so callers can surface them
    in an SSE warning frame.
    """
    if not metadata or overwrite_flags is None:
        return dict(metadata), []

    applied: dict[str, int] = {}
    dropped: list[str] = []
    for key, value in metadata.items():
        flag_name = f"overwrite_{object_kind}_{key}"
        if hasattr(overwrite_flags, flag_name) and getattr(overwrite_flags, flag_name) is False:
            dropped.append(key)
            continue
        applied[key] = value
    return applied, sorted(dropped)

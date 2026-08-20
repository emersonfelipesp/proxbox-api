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


# NetBox's ``description`` is a single-line CharField capped at 200 characters, while a
# Proxmox note is free-form and routinely multi-line. Keep the cap here so all three VM
# payload builders share one rule; three copies of it is exactly how three different
# behaviors arose in the first place.
NETBOX_DESCRIPTION_MAX_CHARS = 200
_TRUNCATION_MARKER = "\u2026"


def derive_description_and_comments(
    raw_description: object,
    *,
    fallback: str,
) -> tuple[str, str | None]:
    """Split a Proxmox note into a NetBox ``description`` and ``comments`` pair.

    A note kept in Proxmox is operator-authored content, so a sync must not destroy it.
    Returns ``(description, comments)`` where:

    * ``description`` is the note's first non-empty line, truncated to NetBox's
      200-character single-line limit with a visible marker, or ``fallback`` when the
      note is absent, blank, or consists solely of ``netbox-metadata`` fences;
    * ``comments`` is the complete cleaned note, but only when it carries more than the
      description does -- multiple lines, or a first line that had to be truncated.
      Otherwise it is ``None``, so a one-line note is not duplicated into two fields.

    ``None`` means *do not write the field*, deliberately not *clear it*. Emitting an
    empty string instead would wipe comments an operator wrote by hand on a VM that
    never had a Proxmox note, which is the exact class of destruction this change
    exists to stop. The cost is that shortening a multi-line Proxmox note to one line
    leaves the previous full note in ``comments`` until it is edited in NetBox; that is
    the safer side of the trade.

    Fences are stripped **unconditionally**, independent of the
    ``parse_description_metadata`` toggle. That toggle governs whether the block's PK
    overrides are applied; it was never about whether the note survives. Before this
    helper the raw note was simply unused when the toggle was off, so nothing leaked --
    now that the note is written to NetBox, an unstripped fence would put raw JSON into
    the rendered UI.
    """
    if not isinstance(raw_description, str):
        return fallback, None

    cleaned = strip_netbox_metadata(raw_description)
    if not cleaned:
        return fallback, None

    # Proxmox notes arrive with either newline convention depending on how they were
    # entered; normalize before splitting so a CRLF note does not leave a stray CR at
    # the end of the description.
    normalized = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in normalized.split("\n")]
    non_empty = [line for line in lines if line]
    if not non_empty:
        return fallback, None

    first_line = non_empty[0]
    truncated = len(first_line) > NETBOX_DESCRIPTION_MAX_CHARS
    if truncated:
        description = (
            first_line[: NETBOX_DESCRIPTION_MAX_CHARS - len(_TRUNCATION_MARKER)]
            + _TRUNCATION_MARKER
        )
    else:
        description = first_line

    full_note = "\n".join(lines).strip()
    has_more = truncated or len(non_empty) > 1
    return description, (full_note if has_more else None)


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

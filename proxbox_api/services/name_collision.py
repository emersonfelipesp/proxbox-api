"""VM name collision resolver.

Deterministic helper for assigning NetBox-unique VM names within a single
NetBox cluster. Two responsibilities:

1.  Pick the smallest free ``" (N)"`` suffix for a candidate name given the
    set of names already taken in the target NetBox cluster.
2.  Detect operator renames: if NetBox already has a ``VirtualMachine`` whose
    ``custom_fields.proxmox_vm_id`` matches the incoming VMID and whose
    current ``name`` is neither the bare candidate nor any algorithmic suffix
    of it, preserve the operator's name instead of renaming back.

Name matching is case-insensitive (``str.casefold``) so we never produce two
NetBox records that differ only by letter case in the same cluster. The
returned ``resolved_name`` preserves the candidate's original casing for
human-facing display.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from proxbox_api.logger import logger


@dataclass(frozen=True)
class NameResolution:
    """Result of a name-collision lookup for one VM."""

    resolved_name: str
    original_name: str
    suffix_index: int
    is_collision: bool
    operator_renamed: bool


_SUFFIX_RE = re.compile(r"^(?P<base>.+?) \((?P<n>\d+)\)$")


def _is_algorithmic_variant(name: str, candidate: str) -> bool:
    """Return True if ``name`` is ``candidate`` or any ``candidate (N)`` form."""
    name_cf = name.casefold()
    cand_cf = candidate.casefold()
    if name_cf == cand_cf:
        return True
    match = _SUFFIX_RE.match(name)
    if not match:
        return False
    return match.group("base").casefold() == cand_cf


def _pick_suffix(candidate: str, used_names_in_cluster: set[str]) -> tuple[str, int]:
    """Return ``(resolved_name, suffix_index)`` for ``candidate``.

    ``suffix_index == 1`` means no suffix was needed. ``2`` produces
    ``"name (2)"``, ``3`` produces ``"name (3)"``, and so on. Matching against
    ``used_names_in_cluster`` is case-insensitive; the resolved name preserves
    the candidate's original casing.
    """
    used_cf = {n.casefold() for n in used_names_in_cluster}
    if candidate.casefold() not in used_cf:
        return candidate, 1
    n = 2
    while True:
        proposed = f"{candidate} ({n})"
        if proposed.casefold() not in used_cf:
            return proposed, n
        n += 1


async def resolve_unique_vm_name(
    nb: object,
    *,
    netbox_cluster_id: int | None,
    proxmox_cluster_name: str,
    candidate: str,
    proxmox_vmid: int,
    used_names_in_cluster: set[str],
    existing_vm_by_vmid: dict[int, dict] | None = None,
    last_synced_proxmox_name: str | None = None,
) -> NameResolution:
    """Resolve a deterministic, NetBox-unique VM name within one NetBox cluster.

    Parameters
    ----------
    nb:
        NetBox client session. Unused when ``existing_vm_by_vmid`` is supplied;
        kept in the signature so individual-sync callers can pass a session and
        defer the lookup if they want.
    netbox_cluster_id:
        NetBox cluster id the VM will land in. ``None`` skips operator-rename
        detection (cluster mapping not yet resolved).
    proxmox_cluster_name:
        Proxmox cluster label, used purely for SSE telemetry.
    candidate:
        Bare Proxmox VM name.
    proxmox_vmid:
        Proxmox VMID, used to look up the NetBox record for operator-rename
        detection.
    used_names_in_cluster:
        Mutable set of names already claimed in the same NetBox cluster during
        this sync pass. Callers must pre-populate it with NetBox-side names
        (filtering out the record this VMID currently owns, if any) and the
        helper registers the result before returning.
    existing_vm_by_vmid:
        Optional pre-built ``{vmid: vm_dict}`` map. When provided, the helper
        consults it instead of touching ``nb``. Bulk callers always pass this
        from the pre-loaded snapshot.

    Returns
    -------
    NameResolution
        ``operator_renamed`` is True iff the NetBox record at ``proxmox_vmid``
        has a custom name that does not look algorithmic. In that case the
        caller should emit a warning frame and skip the rename.
    """
    del nb  # operator-rename lookup uses the pre-built snapshot only

    existing = (existing_vm_by_vmid or {}).get(proxmox_vmid)
    operator_renamed = False
    if existing is not None and netbox_cluster_id is not None:
        existing_name = str(existing.get("name", ""))
        if existing_name and not _is_algorithmic_variant(existing_name, candidate):
            # "Stored NetBox name differs from the incoming Proxmox name" has two
            # possible causes and, on its own, cannot tell them apart:
            #   * an operator renamed the VM inside NetBox   -> keep their edit
            #   * somebody renamed the VM in Proxmox         -> apply the rename
            #
            # `last_synced_proxmox_name` is the name Proxmox reported at the last
            # successful sync, which disambiguates them. If the stored name still
            # matches what Proxmox last said, nobody has touched it in NetBox and
            # the difference must come from the Proxmox side, so the new name
            # wins (netbox-proxbox issue #617).
            #
            # When it is absent -- the sidecar API is unavailable, or the row has
            # not been re-synced since the field was added, which is every row on
            # first upgrade -- fall back to the historical assumption. Preserving
            # a name we are unsure about is the safer failure: it loses a rename,
            # where the opposite would destroy an operator's deliberate edit.
            renamed_in_proxmox = (
                bool(last_synced_proxmox_name) and existing_name == last_synced_proxmox_name
            )
            if not renamed_in_proxmox:
                used_names_in_cluster.add(existing_name)
                logger.info(
                    "name_collision: operator-renamed VM detected vmid=%s candidate=%r netbox_name=%r",
                    proxmox_vmid,
                    candidate,
                    existing_name,
                )
                return NameResolution(
                    resolved_name=existing_name,
                    original_name=candidate,
                    suffix_index=1,
                    is_collision=False,
                    operator_renamed=True,
                )
            logger.info(
                "name_collision: Proxmox-side rename accepted vmid=%s %r -> %r "
                "(last synced name matched the stored NetBox name)",
                proxmox_vmid,
                existing_name,
                candidate,
            )
            # The stored name is about to be replaced, so it must not stay in the
            # used-name set -- otherwise this VM's own old name could push its new
            # name to a " (2)" suffix.
            used_names_in_cluster.discard(existing_name)

    resolved, idx = _pick_suffix(candidate, used_names_in_cluster)
    used_names_in_cluster.add(resolved)
    return NameResolution(
        resolved_name=resolved,
        original_name=candidate,
        suffix_index=idx,
        is_collision=idx > 1,
        operator_renamed=operator_renamed,
    )

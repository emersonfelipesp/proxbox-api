"""Cross-cluster guard for endpoint/vmid-keyed NetBox VM matches.

Proxmox VMIDs are only unique inside one cluster, and the Proxmox endpoint id
stored on a VM's sync-state sidecar comes from an id space that is independent
per deployment (proxbox-api database ids or NetBox plugin primary keys). Two
clusters can therefore legitimately expose the same ``(endpoint id, vmid)`` key
in NetBox. Every lookup keyed on that pair must confirm that the matched NetBox
VM lives in the cluster currently being synchronized before any write targets
it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from proxbox_api.logger import logger
from proxbox_api.services.sync.vm_helpers import relation_id, relation_name

ClusterVerdict = Literal["match", "mismatch", "unassigned", "unknown"]


def _cluster_name_key(value: object) -> str:
    return (relation_name(value) or "").strip().casefold()


def vm_record_cluster_label(record: dict[str, object]) -> str:
    """Describe a NetBox VM record's cluster for diagnostics."""
    cluster = record.get("cluster")
    return f"id={relation_id(cluster)} name={relation_name(cluster) or '?'}"


def _positive_comparison(
    record_cluster: object,
    cluster_id: int | None,
    cluster_name: object,
) -> Literal["match", "mismatch"] | None:
    """Compare ids, then names, returning ``None`` when neither side is comparable."""
    record_cluster_id = relation_id(record_cluster)
    if record_cluster_id is not None and cluster_id is not None:
        return "match" if record_cluster_id == cluster_id else "mismatch"
    record_name = _cluster_name_key(record_cluster)
    live_name = _cluster_name_key(cluster_name)
    if record_name and live_name:
        return "match" if record_name == live_name else "mismatch"
    return None


def vm_cluster_verdict(
    record: dict[str, object],
    *,
    cluster_id: int | None,
    cluster_name: object = None,
) -> ClusterVerdict:
    """Classify a NetBox VM record against the cluster being synchronized.

    ``match`` and ``mismatch`` are positive comparisons: the NetBox cluster id
    is authoritative when both sides know it, otherwise the casefolded names
    are compared. ``unassigned`` means the row carries an explicit ``null``
    cluster (a legacy VM that predates cluster relations). It is *not* a
    verified match: any cluster that collides on ``(endpoint id, vmid)`` could
    adopt it, so it is never selectable for a known live cluster and callers
    treat it like ``unknown``. ``unknown`` means the row has data that
    names no cluster at all (the field is absent or has neither id nor name)
    while the live cluster is known, so the row cannot be verified. When the
    live cluster itself is unknown there is nothing to verify against and the
    result is ``match``.
    """
    record_cluster = record.get("cluster")
    compared = _positive_comparison(record_cluster, cluster_id, cluster_name)
    if compared is not None:
        return compared
    if cluster_id is None and not _cluster_name_key(cluster_name):
        return "match"
    if "cluster" in record and record_cluster is None:
        return "unassigned"
    return "unknown"


def vm_record_in_cluster(
    record: dict[str, object],
    *,
    cluster_id: int | None,
    cluster_name: object = None,
) -> bool:
    """Return ``True`` only when the record is verifiably usable for this cluster.

    A positive mismatch and an unverifiable record (see
    :func:`vm_cluster_verdict`) are both rejected: an unverifiable row could
    belong to another cluster that collides on ``(endpoint id, vmid)``, and an
    UPDATE could reassign it. Explicitly unassigned legacy rows are rejected
    too, because any colliding cluster could otherwise adopt them.
    """
    return vm_cluster_verdict(record, cluster_id=cluster_id, cluster_name=cluster_name) == "match"


def filter_vm_records_in_cluster(
    records: Iterable[dict[str, object]],
    *,
    cluster_id: int | None,
    cluster_name: object = None,
) -> list[dict[str, object]]:
    """Keep only records verifiably usable for the live cluster."""
    return [
        record
        for record in records
        if vm_record_in_cluster(record, cluster_id=cluster_id, cluster_name=cluster_name)
    ]


def log_cross_cluster_rejection(
    record: dict[str, object],
    *,
    vmid: int | None,
    endpoint_id: int | None,
    cluster_id: int | None,
    cluster_name: object,
    context: str,
) -> None:
    """Warn that an endpoint/vmid match belongs to a different cluster."""
    logger.warning(
        "Rejecting %s VM match for vmid=%s endpoint_id=%s: NetBox VM id=%s belongs to "
        "cluster (%s), expected cluster id=%s name=%s; the record will not be written",
        context,
        vmid,
        endpoint_id,
        record.get("id"),
        vm_record_cluster_label(record),
        cluster_id,
        relation_name(cluster_name) or "?",
    )


def log_unverifiable_vm_skip(
    record: dict[str, object],
    *,
    vmid: int | None,
    endpoint_id: int | None,
    cluster_id: int | None,
    cluster_name: object,
) -> None:
    """Warn that a write was skipped because the only candidate has no cluster data."""
    logger.warning(
        "Skipping VM write for vmid=%s endpoint_id=%s expected cluster id=%s name=%s: "
        "NetBox VM id=%s matches the endpoint key but its cluster (%s) cannot be "
        "verified, so it is neither updated nor duplicated",
        vmid,
        endpoint_id,
        cluster_id,
        relation_name(cluster_name) or "?",
        record.get("id"),
        vm_record_cluster_label(record),
    )

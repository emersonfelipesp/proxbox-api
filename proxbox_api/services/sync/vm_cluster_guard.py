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

from proxbox_api.logger import logger
from proxbox_api.services.sync.vm_helpers import relation_id, relation_name


def _cluster_name_key(value: object) -> str:
    return (relation_name(value) or "").strip().casefold()


def vm_record_cluster_label(record: dict[str, object]) -> str:
    """Describe a NetBox VM record's cluster for diagnostics."""
    cluster = record.get("cluster")
    return f"id={relation_id(cluster)} name={relation_name(cluster) or '?'}"


def vm_record_in_cluster(
    record: dict[str, object],
    *,
    cluster_id: int | None,
    cluster_name: object = None,
) -> bool:
    """Return ``False`` only on a positive cluster mismatch.

    The NetBox cluster id is authoritative when both sides know it. When either
    id is unknown, the casefolded cluster names are compared. When neither
    comparison is possible the match is kept: an unknown cluster is not
    evidence of a different cluster, and rejecting it would break legacy
    records that predate cluster relations.
    """
    record_cluster = record.get("cluster")
    record_cluster_id = relation_id(record_cluster)
    if record_cluster_id is not None and cluster_id is not None:
        return record_cluster_id == cluster_id
    record_name = _cluster_name_key(record_cluster)
    live_name = _cluster_name_key(cluster_name)
    if record_name and live_name:
        return record_name == live_name
    return True


def filter_vm_records_in_cluster(
    records: Iterable[dict[str, object]],
    *,
    cluster_id: int | None,
    cluster_name: object = None,
) -> list[dict[str, object]]:
    """Keep only records that are not positively in a different cluster."""
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

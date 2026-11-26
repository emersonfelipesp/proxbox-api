"""Static contract between proxbox-api plugin calls and netbox-proxbox OpenAPI."""

from __future__ import annotations

import argparse
import ast
import copy
import importlib.util
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

PREFIX = "/api/plugins/proxbox/"
ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/netbox_proxbox_0_0_32_post1_contract.json"
EXPECTED_METADATA = {
    "plugin_version": "0.0.32.post1",
    "plugin_wheel_sha256": "4a4a12d6b50a8936b338be5816886c867e9fb144d4ddfa1bbb751fdb83074bdf",
    "image_digest": "sha256:0f3582396d8b7ca5540feb977cc386754b67693bb772a1f4a4209cc1bebb4309",
}
DYNAMIC_PATH_ALLOWLIST: dict[str, str] = {}
NON_EXECUTABLE_LITERAL_ALLOWLIST = {
    "proxbox_api/proxmox_to_netbox/models.py:552": "model URL emitted for a later transport",
    "proxbox_api/services/sync/cloudinit.py:1": "module documentation names the audited route",
    "proxbox_api/services/hardware_discovery.py:66": "credential transport documentation names the audited route",
}
# (method, detail path); None is a path-only capability probe.
HELPERS: dict[str, tuple[tuple[str | None, bool], ...]] = {
    "rest_list_async": (("GET", False),),
    "rest_first_async": (("GET", False),),
    "rest_list_paginated_async": (("GET", False),),
    "rest_get_fresh_json": (("GET", False),),
    "rest_probe_async": ((None, False),),
    "rest_options_async": ((None, False),),
    "rest_create_async": (("POST", False),),
    "rest_ensure_async": (("GET", False), ("POST", False)),
    "rest_reconcile_async": (("GET", False), ("POST", False), ("PATCH", True)),
    "rest_reconcile_async_with_status": (("GET", False), ("POST", False), ("PATCH", True)),
    "rest_patch_async": (("PATCH", True),),
    "rest_bulk_create_async": (("POST", False),),
    "rest_bulk_patch_async": (("PATCH", False),),
    "rest_bulk_delete_async": (("DELETE", False), ("DELETE", True)),
    "rest_bulk_reconcile_async": (
        ("GET", False),
        ("POST", False),
        ("PATCH", False),
        ("PATCH", True),
    ),
    "rest_bulk_reconcile_phases_async": (
        ("GET", False),
        ("POST", False),
        ("PATCH", False),
        ("PATCH", True),
    ),
    "open": (("GET", False),),
    "urlopen": (("GET", False),),
}


@dataclass(frozen=True)
class WrapperContract:
    """REST behavior forwarded by one project-local wrapper."""

    operations: tuple[tuple[str | None, bool], ...]
    implicit_filters: tuple[str, ...] = ()
    remap_lookup: bool = False
    parent_filter: bool = False
    fixed_path: str | None = None
    body_functions: tuple[str, ...] = ()


RECONCILE_OPERATIONS = HELPERS["rest_reconcile_async"]
WRAPPERS: dict[str, WrapperContract] = {
    "_best_effort_reconcile": WrapperContract(
        RECONCILE_OPERATIONS,
        ("limit", "offset"),
        remap_lookup=True,
        body_functions=("_best_effort_reconcile",),
    ),
    "_fresh_page": WrapperContract(
        HELPERS["rest_get_fresh_json"],
        ("limit",),
        body_functions=("_fresh_page", "_fresh_json"),
    ),
    "_load_selected_netbox_endpoints": WrapperContract(
        HELPERS["rest_list_async"],
        ("id",),
        body_functions=("_load_selected_netbox_endpoints",),
    ),
    "_lookup_parent_sidecar": WrapperContract(
        HELPERS["rest_first_async"],
        ("limit",),
        parent_filter=True,
        body_functions=("_lookup_parent_sidecar",),
    ),
    "_plugin_upsert": WrapperContract(
        RECONCILE_OPERATIONS,
        ("limit", "offset"),
        body_functions=("_plugin_upsert", "_reconcile"),
    ),
    "_plugin_upsert_counted": WrapperContract(
        RECONCILE_OPERATIONS,
        ("limit", "offset"),
        body_functions=("_plugin_upsert_counted", "_plugin_upsert", "_reconcile"),
    ),
    "_request_settings_json": WrapperContract(
        HELPERS["urlopen"],
        body_functions=("_request_settings_json",),
    ),
    "_reconcile_task_payloads": WrapperContract(
        (("GET", False), ("POST", False), ("PATCH", False)),
        ("limit", "offset", "upid"),
        fixed_path="/api/plugins/proxbox/task-history/",
        body_functions=("_reconcile_task_payloads",),
    ),
    "_upsert_parent_sidecar": WrapperContract(
        RECONCILE_OPERATIONS,
        ("limit", "offset"),
        parent_filter=True,
        body_functions=(
            "_upsert_parent_sidecar",
            "_lookup_parent_sidecar",
            "_patch_parent_sidecar",
        ),
    ),
    "get_serialized_first_record": WrapperContract(
        HELPERS["rest_first_async"],
        body_functions=("get_serialized_first_record",),
    ),
}
PATH_ONLY_HELPERS = {
    "_handle_sidecar_write_failure",
    "_normalize_parent_id",
    "_urlopen_kwargs",
    "clear_rest_get_cache_for_path",
    "discard",
}
POSITIONAL_PATH_HELPERS = {"open", "urlopen", *PATH_ONLY_HELPERS}
QUERY_ARGUMENTS = {"base_query", "filters", "lookup", "params", "query"}

# Exact route/filter coverage extracted from HEAD 127df044. Empty tuples retain
# routes that the old extractor checked without a statically known query key.
HEAD_ROUTE_FILTERS: dict[str, tuple[str, ...]] = {
    "/api/plugins/proxbox/backup-routines/": ("endpoint", "job_id", "limit", "offset"),
    "/api/plugins/proxbox/backups/": (
        "limit",
        "offset",
        "virtual_machine",
        "vmid",
        "volume_id",
    ),
    "/api/plugins/proxbox/cloud-image-templates/": ("is_active", "limit"),
    "/api/plugins/proxbox/endpoints/proxmox/": ("id", "limit"),
    "/api/plugins/proxbox/endpoints/proxmox/{dynamic}/sync-cluster/": (),
    "/api/plugins/proxbox/guest-vm-interface-addresses/": (
        "guest_interface",
        "ip_address",
        "limit",
        "offset",
    ),
    "/api/plugins/proxbox/guest-vm-interfaces/": (
        "limit",
        "name",
        "offset",
        "virtual_machine",
    ),
    "/api/plugins/proxbox/nodes/": ("name",),
    "/api/plugins/proxbox/proxmox-clusters/": (),
    "/api/plugins/proxbox/proxmox-nodes/": ("name", "proxmox_cluster_id"),
    "/api/plugins/proxbox/replications/": (
        "endpoint",
        "limit",
        "offset",
        "replication_id",
        "status",
    ),
    "/api/plugins/proxbox/sdn-bindings/": (
        "cluster_name",
        "endpoint",
        "source_name",
        "source_type",
    ),
    "/api/plugins/proxbox/sdn-controllers/": (
        "cluster_name",
        "controller_name",
        "endpoint",
    ),
    "/api/plugins/proxbox/sdn-fabrics/": ("cluster_name", "endpoint", "fabric_name"),
    "/api/plugins/proxbox/sdn-prefix-lists/": (
        "cidr",
        "cluster_name",
        "endpoint",
        "name",
    ),
    "/api/plugins/proxbox/sdn-route-maps/": (
        "cluster_name",
        "endpoint",
        "name",
        "order",
    ),
    "/api/plugins/proxbox/sdn-subnets/": (
        "cluster_name",
        "endpoint",
        "subnet",
        "vnet_name",
    ),
    "/api/plugins/proxbox/sdn-vnets/": ("cluster_name", "endpoint", "vnet_name"),
    "/api/plugins/proxbox/sdn-zones/": ("cluster_name", "endpoint", "zone_name"),
    "/api/plugins/proxbox/settings/": (),
    "/api/plugins/proxbox/settings/runtime/": (),
    "/api/plugins/proxbox/snapshots/": ("limit", "name", "node", "offset", "vmid"),
    "/api/plugins/proxbox/storage/": ("cluster", "limit", "name", "offset"),
    "/api/plugins/proxbox/sync-state/clusters/": (),
    "/api/plugins/proxbox/sync-state/devices/": (
        "limit",
        "proxmox_cluster_name",
        "proxmox_endpoint_raw_id",
        "proxmox_node_name",
    ),
    "/api/plugins/proxbox/sync-state/virtual-disks/": (),
    "/api/plugins/proxbox/sync-state/virtual-machines/": (),
    "/api/plugins/proxbox/sync-state/vm-interfaces/": (),
    "/api/plugins/proxbox/task-history/": ("limit", "offset", "upid"),
    "/api/plugins/proxbox/vm-cloudinit/": ("limit", "offset", "virtual_machine_id"),
}

CALL_SITE_FLOOR_ROWS: tuple[tuple[str, str, str, str | None, tuple[str | None, ...]], ...] = (
    (
        "proxbox_api/e2e/session.py",
        "cleanup_e2e_objects",
        "/api/plugins/proxbox/backups/",
        "GET",
        ("limit",),
    ),
    (
        "proxbox_api/e2e/session.py",
        "cleanup_e2e_objects",
        "/api/plugins/proxbox/backups/{dynamic}/",
        "DELETE",
        (None,),
    ),
    (
        "proxbox_api/plugin_key_authority.py",
        "open",
        "/api/plugins/proxbox/settings/runtime/",
        "GET",
        (None,),
    ),
    (
        "proxbox_api/routes/cloud/provision.py",
        "rest_list_async",
        "/api/plugins/proxbox/endpoints/proxmox/",
        "GET",
        ("limit",),
    ),
    (
        "proxbox_api/routes/cloud/templates.py",
        "rest_list_async",
        "/api/plugins/proxbox/cloud-image-templates/",
        "GET",
        ("is_active", "limit"),
    ),
    (
        "proxbox_api/routes/proxmox/services.py",
        "_urlopen_kwargs",
        "/api/plugins/proxbox/endpoints/proxmox/{dynamic}/",
        None,
        (None,),
    ),
    (
        "proxbox_api/routes/proxmox/services.py",
        "urlopen",
        "/api/plugins/proxbox/endpoints/proxmox/{dynamic}/",
        "GET",
        (None,),
    ),
    (
        "proxbox_api/routes/virtualization/virtual_machines/backups_vm.py",
        "clear_rest_get_cache_for_path",
        "/api/plugins/proxbox/backups/",
        None,
        (None,),
    ),
    (
        "proxbox_api/routes/virtualization/virtual_machines/backups_vm.py",
        "rest_bulk_create_async",
        "/api/plugins/proxbox/backups/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/routes/virtualization/virtual_machines/backups_vm.py",
        "rest_bulk_delete_async",
        "/api/plugins/proxbox/backups/",
        "DELETE",
        (None,),
    ),
    (
        "proxbox_api/routes/virtualization/virtual_machines/backups_vm.py",
        "rest_bulk_delete_async",
        "/api/plugins/proxbox/backups/{dynamic}/",
        "DELETE",
        (None,),
    ),
    (
        "proxbox_api/routes/virtualization/virtual_machines/backups_vm.py",
        "rest_bulk_patch_async",
        "/api/plugins/proxbox/backups/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/routes/virtualization/virtual_machines/backups_vm.py",
        "rest_list_async",
        "/api/plugins/proxbox/storage/",
        "GET",
        (None,),
    ),
    (
        "proxbox_api/routes/virtualization/virtual_machines/backups_vm.py",
        "rest_list_paginated_async",
        "/api/plugins/proxbox/backups/",
        "GET",
        ("limit", "offset"),
    ),
    (
        "proxbox_api/routes/virtualization/virtual_machines/backups_vm.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/backups/",
        "GET",
        ("limit", "offset", "virtual_machine", "volume_id"),
    ),
    (
        "proxbox_api/routes/virtualization/virtual_machines/backups_vm.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/backups/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/routes/virtualization/virtual_machines/backups_vm.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/backups/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/routes/virtualization/virtual_machines/sync_vm.py",
        "rest_list_async",
        "/api/plugins/proxbox/storage/",
        "GET",
        (None,),
    ),
    (
        "proxbox_api/routes/virtualization/virtual_machines/sync_vm.py",
        "rest_list_async",
        "/api/plugins/proxbox/sync-state/devices/",
        "GET",
        ("limit", "proxmox_cluster_name", "proxmox_endpoint_raw_id", "proxmox_node_name"),
    ),
    (
        "proxbox_api/services/hardware_discovery.py",
        "urlopen",
        "/api/plugins/proxbox/ssh-credentials/by-node/{dynamic}/credentials/",
        "GET",
        (None,),
    ),
    (
        "proxbox_api/services/ssh_terminal.py",
        "_urlopen_kwargs",
        "/api/plugins/proxbox/ssh-credentials/by-endpoint/{dynamic}/credentials/",
        None,
        (None,),
    ),
    (
        "proxbox_api/services/ssh_terminal.py",
        "urlopen",
        "/api/plugins/proxbox/ssh-credentials/by-endpoint/{dynamic}/credentials/",
        "GET",
        (None,),
    ),
    (
        "proxbox_api/services/sync/backup_routines.py",
        "rest_bulk_patch_async",
        "/api/plugins/proxbox/backup-routines/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/backup_routines.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/backup-routines/",
        "GET",
        ("endpoint", "job_id", "limit", "offset"),
    ),
    (
        "proxbox_api/services/sync/backup_routines.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/backup-routines/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/backup_routines.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/backup-routines/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/backup_routines.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/backup-routines/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/backup_routines.py",
        "rest_list_async",
        "/api/plugins/proxbox/endpoints/proxmox/",
        "GET",
        (None,),
    ),
    (
        "proxbox_api/services/sync/backup_routines.py",
        "rest_list_paginated_async",
        "/api/plugins/proxbox/backup-routines/",
        "GET",
        ("limit", "offset"),
    ),
    (
        "proxbox_api/services/sync/cloudinit.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/vm-cloudinit/",
        "GET",
        ("limit", "offset", "virtual_machine_id"),
    ),
    (
        "proxbox_api/services/sync/cloudinit.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/vm-cloudinit/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/cloudinit.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/vm-cloudinit/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/cluster_links.py",
        "_fresh_page",
        "/api/plugins/proxbox/endpoints/proxmox/",
        "GET",
        ("limit",),
    ),
    (
        "proxbox_api/services/sync/cluster_links.py",
        "_fresh_page",
        "/api/plugins/proxbox/proxmox-clusters/",
        "GET",
        ("limit",),
    ),
    (
        "proxbox_api/services/sync/cluster_links.py",
        "rest_create_async",
        "/api/plugins/proxbox/endpoints/proxmox/{dynamic}/sync-cluster/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/cluster_links.py",
        "rest_list_async",
        "/api/plugins/proxbox/endpoints/proxmox/",
        "GET",
        (None,),
    ),
    (
        "proxbox_api/services/sync/cluster_links.py",
        "rest_probe_async",
        "/api/plugins/proxbox/endpoints/proxmox/{dynamic}/sync-cluster/",
        None,
        (None,),
    ),
    (
        "proxbox_api/services/sync/guest_vm_interface.py",
        "_best_effort_reconcile",
        "/api/plugins/proxbox/guest-vm-interface-addresses/",
        "GET",
        ("guest_interface", "guest_interface_id", "ip_address", "ip_address_id", "limit", "offset"),
    ),
    (
        "proxbox_api/services/sync/guest_vm_interface.py",
        "_best_effort_reconcile",
        "/api/plugins/proxbox/guest-vm-interface-addresses/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/guest_vm_interface.py",
        "_best_effort_reconcile",
        "/api/plugins/proxbox/guest-vm-interface-addresses/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/guest_vm_interface.py",
        "_best_effort_reconcile",
        "/api/plugins/proxbox/guest-vm-interfaces/",
        "GET",
        ("limit", "name", "offset", "virtual_machine", "virtual_machine_id"),
    ),
    (
        "proxbox_api/services/sync/guest_vm_interface.py",
        "_best_effort_reconcile",
        "/api/plugins/proxbox/guest-vm-interfaces/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/guest_vm_interface.py",
        "_best_effort_reconcile",
        "/api/plugins/proxbox/guest-vm-interfaces/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/individual/backup_routine_sync.py",
        "rest_list_async",
        "/api/plugins/proxbox/backup-routines/",
        "GET",
        ("endpoint", "job_id"),
    ),
    (
        "proxbox_api/services/sync/individual/backup_routine_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/backup-routines/",
        "GET",
        ("endpoint", "job_id", "limit", "offset"),
    ),
    (
        "proxbox_api/services/sync/individual/backup_routine_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/backup-routines/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/individual/backup_routine_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/backup-routines/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/individual/backup_sync.py",
        "get_serialized_first_record",
        "/api/plugins/proxbox/backups/",
        "GET",
        ("vmid", "volume_id"),
    ),
    (
        "proxbox_api/services/sync/individual/backup_sync.py",
        "rest_list_async",
        "/api/plugins/proxbox/backups/",
        "GET",
        ("vmid", "volume_id"),
    ),
    (
        "proxbox_api/services/sync/individual/backup_sync.py",
        "rest_list_async",
        "/api/plugins/proxbox/storage/",
        "GET",
        ("name",),
    ),
    (
        "proxbox_api/services/sync/individual/backup_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/backups/",
        "GET",
        ("limit", "offset", "vmid", "volume_id"),
    ),
    (
        "proxbox_api/services/sync/individual/backup_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/backups/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/individual/backup_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/backups/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/individual/replication_sync.py",
        "get_serialized_first_record",
        "/api/plugins/proxbox/replications/",
        "GET",
        ("replication_id",),
    ),
    (
        "proxbox_api/services/sync/individual/replication_sync.py",
        "rest_list_async",
        "/api/plugins/proxbox/nodes/",
        "GET",
        ("name",),
    ),
    (
        "proxbox_api/services/sync/individual/replication_sync.py",
        "rest_list_async",
        "/api/plugins/proxbox/replications/",
        "GET",
        ("replication_id",),
    ),
    (
        "proxbox_api/services/sync/individual/replication_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/replications/",
        "GET",
        ("limit", "offset", "replication_id"),
    ),
    (
        "proxbox_api/services/sync/individual/replication_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/replications/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/individual/replication_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/replications/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/individual/snapshot_sync.py",
        "rest_list_async",
        "/api/plugins/proxbox/snapshots/",
        "GET",
        ("name", "node", "vmid"),
    ),
    (
        "proxbox_api/services/sync/individual/snapshot_sync.py",
        "rest_list_async",
        "/api/plugins/proxbox/snapshots/",
        "GET",
        ("name", "vmid"),
    ),
    (
        "proxbox_api/services/sync/individual/snapshot_sync.py",
        "rest_list_async",
        "/api/plugins/proxbox/storage/",
        "GET",
        ("name",),
    ),
    (
        "proxbox_api/services/sync/individual/snapshot_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/snapshots/",
        "GET",
        ("limit", "name", "node", "offset", "vmid"),
    ),
    (
        "proxbox_api/services/sync/individual/snapshot_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/snapshots/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/individual/snapshot_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/snapshots/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/individual/storage_sync.py",
        "rest_list_async",
        "/api/plugins/proxbox/storage/",
        "GET",
        ("cluster", "name"),
    ),
    (
        "proxbox_api/services/sync/individual/storage_sync.py",
        "rest_list_async",
        "/api/plugins/proxbox/storage/",
        "GET",
        ("name",),
    ),
    (
        "proxbox_api/services/sync/individual/storage_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/storage/",
        "GET",
        ("cluster", "limit", "name", "offset"),
    ),
    (
        "proxbox_api/services/sync/individual/storage_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/storage/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/individual/storage_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/storage/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/individual/task_history_sync.py",
        "rest_list_async",
        "/api/plugins/proxbox/task-history/",
        "GET",
        ("upid",),
    ),
    (
        "proxbox_api/services/sync/individual/task_history_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/task-history/",
        "GET",
        ("limit", "offset", "upid"),
    ),
    (
        "proxbox_api/services/sync/individual/task_history_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/task-history/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/individual/task_history_sync.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/task-history/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/individual/virtual_disk_sync.py",
        "rest_list_async",
        "/api/plugins/proxbox/storage/",
        "GET",
        ("name",),
    ),
    (
        "proxbox_api/services/sync/replications.py",
        "rest_bulk_patch_async",
        "/api/plugins/proxbox/replications/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/replications.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/replications/",
        "GET",
        ("endpoint", "limit", "offset", "replication_id"),
    ),
    (
        "proxbox_api/services/sync/replications.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/replications/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/replications.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/replications/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/replications.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/replications/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/replications.py",
        "rest_list_async",
        "/api/plugins/proxbox/nodes/",
        "GET",
        (None,),
    ),
    (
        "proxbox_api/services/sync/replications.py",
        "rest_list_paginated_async",
        "/api/plugins/proxbox/replications/",
        "GET",
        ("endpoint", "limit", "offset", "status"),
    ),
    (
        "proxbox_api/services/sync/role_resolution.py",
        "rest_first_async",
        "/api/plugins/proxbox/endpoints/proxmox/",
        "GET",
        ("id",),
    ),
    (
        "proxbox_api/services/sync/role_resolution.py",
        "rest_first_async",
        "/api/plugins/proxbox/proxmox-nodes/",
        "GET",
        ("name", "proxmox_cluster_id"),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert",
        "/api/plugins/proxbox/sdn-bindings/",
        "GET",
        ("cluster_name", "endpoint", "limit", "offset", "source_name", "source_type"),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert",
        "/api/plugins/proxbox/sdn-bindings/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert",
        "/api/plugins/proxbox/sdn-bindings/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-bindings/",
        "GET",
        ("cluster_name", "endpoint", "limit", "offset", "source_name", "source_type"),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-bindings/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-bindings/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-controllers/",
        "GET",
        ("cluster_name", "controller_name", "endpoint", "limit", "offset"),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-controllers/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-controllers/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-fabrics/",
        "GET",
        ("cluster_name", "endpoint", "fabric_name", "limit", "offset"),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-fabrics/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-fabrics/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-prefix-lists/",
        "GET",
        ("cidr", "cluster_name", "endpoint", "limit", "name", "offset"),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-prefix-lists/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-prefix-lists/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-route-maps/",
        "GET",
        ("cluster_name", "endpoint", "limit", "name", "offset", "order"),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-route-maps/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-route-maps/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-subnets/",
        "GET",
        ("cluster_name", "endpoint", "limit", "offset", "subnet", "vnet_name"),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-subnets/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-subnets/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-vnets/",
        "GET",
        ("cluster_name", "endpoint", "limit", "offset", "vnet_name"),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-vnets/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-vnets/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-zones/",
        "GET",
        ("cluster_name", "endpoint", "limit", "offset", "zone_name"),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-zones/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "_plugin_upsert_counted",
        "/api/plugins/proxbox/sdn-zones/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sdn.py",
        "rest_first_async",
        "/api/plugins/proxbox/endpoints/proxmox/",
        "GET",
        ("id", "limit"),
    ),
    (
        "proxbox_api/services/sync/snapshots.py",
        "rest_bulk_delete_async",
        "/api/plugins/proxbox/snapshots/",
        "DELETE",
        (None,),
    ),
    (
        "proxbox_api/services/sync/snapshots.py",
        "rest_bulk_delete_async",
        "/api/plugins/proxbox/snapshots/{dynamic}/",
        "DELETE",
        (None,),
    ),
    (
        "proxbox_api/services/sync/snapshots.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/snapshots/",
        "GET",
        ("limit", "name", "node", "offset", "virtual_machine", "vmid"),
    ),
    (
        "proxbox_api/services/sync/snapshots.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/snapshots/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/snapshots.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/snapshots/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/snapshots.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/snapshots/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/snapshots.py",
        "rest_list_async",
        "/api/plugins/proxbox/snapshots/",
        "GET",
        (None,),
    ),
    (
        "proxbox_api/services/sync/snapshots.py",
        "rest_list_async",
        "/api/plugins/proxbox/storage/",
        "GET",
        (None,),
    ),
    (
        "proxbox_api/services/sync/storages.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/storage/",
        "GET",
        ("cluster", "limit", "name", "offset"),
    ),
    (
        "proxbox_api/services/sync/storages.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/storage/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/storages.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/storage/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/storages.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/storage/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_handle_sidecar_write_failure",
        "/api/plugins/proxbox/sync-state/devices/",
        None,
        (None,),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_lookup_parent_sidecar",
        "/api/plugins/proxbox/sync-state/devices/",
        "GET",
        ("device", "device_id", "limit"),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_normalize_parent_id",
        "/api/plugins/proxbox/sync-state/virtual-machines/",
        None,
        (None,),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_upsert_parent_sidecar",
        "/api/plugins/proxbox/sync-state/clusters/",
        "GET",
        ("cluster", "cluster_id", "limit", "offset"),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_upsert_parent_sidecar",
        "/api/plugins/proxbox/sync-state/clusters/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_upsert_parent_sidecar",
        "/api/plugins/proxbox/sync-state/clusters/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_upsert_parent_sidecar",
        "/api/plugins/proxbox/sync-state/devices/",
        "GET",
        ("device", "device_id", "limit", "offset"),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_upsert_parent_sidecar",
        "/api/plugins/proxbox/sync-state/devices/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_upsert_parent_sidecar",
        "/api/plugins/proxbox/sync-state/devices/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_upsert_parent_sidecar",
        "/api/plugins/proxbox/sync-state/virtual-disks/",
        "GET",
        ("limit", "offset", "virtual_disk", "virtual_disk_id"),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_upsert_parent_sidecar",
        "/api/plugins/proxbox/sync-state/virtual-disks/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_upsert_parent_sidecar",
        "/api/plugins/proxbox/sync-state/virtual-disks/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_upsert_parent_sidecar",
        "/api/plugins/proxbox/sync-state/virtual-machines/",
        "GET",
        ("limit", "offset", "virtual_machine", "virtual_machine_id"),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_upsert_parent_sidecar",
        "/api/plugins/proxbox/sync-state/virtual-machines/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_upsert_parent_sidecar",
        "/api/plugins/proxbox/sync-state/virtual-machines/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_upsert_parent_sidecar",
        "/api/plugins/proxbox/sync-state/vm-interfaces/",
        "GET",
        ("limit", "offset", "vm_interface", "vm_interface_id"),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_upsert_parent_sidecar",
        "/api/plugins/proxbox/sync-state/vm-interfaces/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "_upsert_parent_sidecar",
        "/api/plugins/proxbox/sync-state/vm-interfaces/{dynamic}/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "clear_rest_get_cache_for_path",
        "/api/plugins/proxbox/sync-state/devices/",
        None,
        (None,),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "clear_rest_get_cache_for_path",
        "/api/plugins/proxbox/sync-state/virtual-machines/",
        None,
        (None,),
    ),
    (
        "proxbox_api/services/sync/sync_state_writer.py",
        "discard",
        "/api/plugins/proxbox/sync-state/virtual-machines/",
        None,
        (None,),
    ),
    (
        "proxbox_api/services/sync/task_history.py",
        "_reconcile_task_payloads",
        "/api/plugins/proxbox/task-history/",
        "GET",
        ("limit", "offset", "upid"),
    ),
    (
        "proxbox_api/services/sync/task_history.py",
        "_reconcile_task_payloads",
        "/api/plugins/proxbox/task-history/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/task_history.py",
        "_reconcile_task_payloads",
        "/api/plugins/proxbox/task-history/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/task_history.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/task-history/",
        "GET",
        ("limit", "offset", "upid"),
    ),
    (
        "proxbox_api/services/sync/task_history.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/task-history/",
        "PATCH",
        (None,),
    ),
    (
        "proxbox_api/services/sync/task_history.py",
        "rest_bulk_reconcile_async",
        "/api/plugins/proxbox/task-history/",
        "POST",
        (None,),
    ),
    (
        "proxbox_api/services/sync/virtual_disks.py",
        "rest_list_async",
        "/api/plugins/proxbox/storage/",
        "GET",
        (None,),
    ),
    (
        "proxbox_api/session/proxmox_providers.py",
        "_load_selected_netbox_endpoints",
        "/api/plugins/proxbox/endpoints/proxmox/",
        "GET",
        ("id",),
    ),
    (
        "proxbox_api/session/proxmox_providers.py",
        "rest_list_async",
        "/api/plugins/proxbox/endpoints/proxmox/",
        "GET",
        (None,),
    ),
    (
        "proxbox_api/settings_client.py",
        "_request_settings_json",
        "/api/plugins/proxbox/settings/",
        "GET",
        (None,),
    ),
    (
        "proxbox_api/settings_client.py",
        "_request_settings_json",
        "/api/plugins/proxbox/settings/runtime/",
        "GET",
        (None,),
    ),
)

CALL_SITE_FLOOR = frozenset(
    (file, function, route, method, filter_name)
    for file, function, route, method, filters in CALL_SITE_FLOOR_ROWS
    for filter_name in filters
)


OperationKey = tuple[str, str, str, str | None]

# These additive filters are intentionally sent to old netbox-proxbox releases,
# which ignore unknown filters and return an unfiltered list. The backend must
# verify every returned row client-side; see the Proxmox VE 9.3 guest UID tests.
FORWARD_COMPATIBLE_FILTERS = {
    ("/api/plugins/proxbox/sync-state/virtual-machines/", "proxmox_cluster_uid"),
    ("/api/plugins/proxbox/sync-state/virtual-machines/", "proxmox_vm_uid"),
}


def _reconcile_bindings(file: str, function: str, route: str) -> tuple[OperationKey, ...]:
    return (
        (file, function, route, "GET"),
        (file, function, route, "POST"),
        (file, function, f"{route.rstrip('/')}/{{dynamic}}/", "PATCH"),
    )


ROUTE_CONSTANT_BINDINGS: dict[str, tuple[OperationKey, ...]] = {
    "proxbox_api/e2e/session.py:206": (
        (
            "proxbox_api/e2e/session.py",
            "cleanup_e2e_objects",
            "/api/plugins/proxbox/backups/",
            "GET",
        ),
        (
            "proxbox_api/e2e/session.py",
            "cleanup_e2e_objects",
            "/api/plugins/proxbox/backups/{dynamic}/",
            "DELETE",
        ),
    ),
    "proxbox_api/plugin_key_authority.py:20": (
        (
            "proxbox_api/plugin_key_authority.py",
            "open",
            "/api/plugins/proxbox/settings/runtime/",
            "GET",
        ),
    ),
    "proxbox_api/routes/cloud/provision.py:65": (
        (
            "proxbox_api/routes/cloud/provision.py",
            "rest_list_async",
            "/api/plugins/proxbox/endpoints/proxmox/",
            "GET",
        ),
    ),
    "proxbox_api/routes/cloud/templates.py:16": (
        (
            "proxbox_api/routes/cloud/templates.py",
            "rest_list_async",
            "/api/plugins/proxbox/cloud-image-templates/",
            "GET",
        ),
    ),
    "proxbox_api/services/sync/cloudinit.py:22": _reconcile_bindings(
        "proxbox_api/services/sync/cloudinit.py",
        "rest_reconcile_async",
        "/api/plugins/proxbox/vm-cloudinit/",
    ),
    "proxbox_api/services/sync/cluster_links.py:30": (
        (
            "proxbox_api/services/sync/cluster_links.py",
            "rest_list_async",
            "/api/plugins/proxbox/endpoints/proxmox/",
            "GET",
        ),
    ),
    "proxbox_api/services/sync/cluster_links.py:32": (
        (
            "proxbox_api/services/sync/cluster_links.py",
            "_fresh_page",
            "/api/plugins/proxbox/proxmox-clusters/",
            "GET",
        ),
    ),
    "proxbox_api/services/sync/guest_vm_interface.py:14": _reconcile_bindings(
        "proxbox_api/services/sync/guest_vm_interface.py",
        "_best_effort_reconcile",
        "/api/plugins/proxbox/guest-vm-interfaces/",
    ),
    "proxbox_api/services/sync/guest_vm_interface.py:15": _reconcile_bindings(
        "proxbox_api/services/sync/guest_vm_interface.py",
        "_best_effort_reconcile",
        "/api/plugins/proxbox/guest-vm-interface-addresses/",
    ),
    **{
        f"proxbox_api/services/sync/sync_state_writer.py:{line}": _reconcile_bindings(
            "proxbox_api/services/sync/sync_state_writer.py",
            "_upsert_parent_sidecar",
            route,
        )
        for line, route in (
            (28, "/api/plugins/proxbox/sync-state/virtual-machines/"),
            (29, "/api/plugins/proxbox/sync-state/devices/"),
            (30, "/api/plugins/proxbox/sync-state/clusters/"),
            (31, "/api/plugins/proxbox/sync-state/virtual-disks/"),
            (32, "/api/plugins/proxbox/sync-state/vm-interfaces/"),
        )
    },
    "proxbox_api/services/sync/task_history.py:36": (
        (
            "proxbox_api/services/sync/task_history.py",
            "rest_bulk_reconcile_async",
            "/api/plugins/proxbox/task-history/",
            "GET",
        ),
        (
            "proxbox_api/services/sync/task_history.py",
            "rest_bulk_reconcile_async",
            "/api/plugins/proxbox/task-history/",
            "POST",
        ),
        (
            "proxbox_api/services/sync/task_history.py",
            "rest_bulk_reconcile_async",
            "/api/plugins/proxbox/task-history/",
            "PATCH",
        ),
    ),
    "proxbox_api/session/proxmox_providers.py:877": (
        (
            "proxbox_api/session/proxmox_providers.py",
            "rest_list_async",
            "/api/plugins/proxbox/endpoints/proxmox/",
            "GET",
        ),
    ),
}


@dataclass(frozen=True, order=True)
class PluginCall:
    file: str
    line: int
    function: str
    path: str
    method: str | None
    filters: tuple[str, ...]


@dataclass(frozen=True, order=True)
class UnresolvedCall:
    file: str
    line: int
    function: str
    reason: str

    @property
    def key(self) -> str:
        return f"{self.file}:{self.line}"


def _name(node: ast.AST) -> str:
    return (
        node.id
        if isinstance(node, ast.Name)
        else node.attr
        if isinstance(node, ast.Attribute)
        else ""
    )


def _scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    while (node := parents.get(node)) is not None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _returns(tree: ast.AST) -> dict[str, ast.AST]:
    result: dict[str, ast.AST] = {}
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        values = [
            node.value for node in ast.walk(function) if isinstance(node, ast.Return) and node.value
        ]
        if len(values) == 1:
            result[function.name] = values[0]
    return result


def _string(
    node: ast.AST, strings: dict[str, str], returns: dict[str, ast.AST], depth: int = 0
) -> str | None:
    if depth > 5:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return strings.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = (
            _string(node.left, strings, returns, depth + 1),
            _string(node.right, strings, returns, depth + 1),
        )
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        return _joined_string(node, strings, returns, depth)
    if isinstance(node, ast.Call) and (value := returns.get(_name(node.func))) is not None:
        return _string(value, strings, returns, depth + 1)
    if isinstance(node, ast.Call) and _name(node.func) == "Request" and node.args:
        return _string(node.args[0], strings, returns, depth + 1)
    return None


def _joined_string(
    node: ast.JoinedStr, strings: dict[str, str], returns: dict[str, ast.AST], depth: int
) -> str | None:
    parts: list[str] = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        elif isinstance(value, ast.FormattedValue):
            parts.append(_string(value.value, strings, returns, depth + 1) or "{dynamic}")
        else:
            return None
    return "".join(parts)


def _dict_keys(node: ast.AST, dictionaries: dict[str, set[str] | None]) -> set[str] | None:
    if isinstance(node, ast.Name):
        return dictionaries.get(node.id)
    if not isinstance(node, ast.Dict):
        return None
    result: set[str] = set()
    for key, value in zip(node.keys, node.values, strict=True):
        if key is None:
            nested = _dict_keys(value, dictionaries)
            if nested is None:
                return None
            result.update(nested)
        elif isinstance(key, ast.Constant) and isinstance(key.value, str):
            result.add(key.value)
        else:
            return None
    return result


def _literal_dict_keys(node: ast.AST) -> set[str]:
    if not isinstance(node, ast.Dict):
        return set()
    keys: set[str] = set()
    for key, value in zip(node.keys, node.values, strict=True):
        if key is None:
            keys.update(_literal_dict_keys(value))
        elif isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
    return keys


UNRESOLVED_WRAPPER_KEY = "<unresolved wrapper query key>"


def _query_flow_names(node: ast.AST) -> set[str]:
    """Return local names whose contents flow into a query argument expression."""
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Dict):
        return {
            name
            for key, value in zip(node.keys, node.values, strict=True)
            if key is None
            for name in _query_flow_names(value)
        }
    return set()


def _mutation_keys(node: ast.AST, names: set[str]) -> set[str]:
    """Return keys a wrapper adds to a query-flowing local dictionary.

    A key that cannot be resolved to a string literal becomes a sentinel that no
    fixture route accepts, so the contract check fails closed.
    """
    if isinstance(node, (ast.Assign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        keys = {_subscript_key(target) for target in targets if _targets_name(target, names)}
        if isinstance(node, ast.AugAssign) and _targets_name(node.target, names, plain=True):
            keys |= _literal_dict_keys(node.value) or {UNRESOLVED_WRAPPER_KEY}
        return keys
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"update", "setdefault"}
        and _targets_name(node.func.value, names, plain=True)
    ):
        if node.func.attr == "setdefault":
            return {_constant_key(node.args[0]) if node.args else UNRESOLVED_WRAPPER_KEY}
        literal = [_literal_dict_keys(arg) for arg in node.args]
        unresolved = any(not keys for keys in literal) or bool(node.keywords and not literal)
        keys = set().union(*literal) | {kw.arg for kw in node.keywords if kw.arg}
        return keys | ({UNRESOLVED_WRAPPER_KEY} if unresolved else set())
    return set()


def _targets_name(node: ast.AST, names: set[str], *, plain: bool = False) -> bool:
    if plain:
        return isinstance(node, ast.Name) and node.id in names
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id in names
    )


def _constant_key(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return UNRESOLVED_WRAPPER_KEY


def _subscript_key(node: ast.AST) -> str:
    return _constant_key(node.slice) if isinstance(node, ast.Subscript) else UNRESOLVED_WRAPPER_KEY


def _wrapper_scope(
    node: ast.AST, parents: dict[ast.AST, ast.AST], names: set[str]
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    scope = _scope(node, parents)
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and scope.name in names:
        return scope
    return None


def _wrapper_body_filters(tree: ast.AST, parents: dict[ast.AST, ast.AST]) -> dict[str, set[str]]:
    names = {name for contract in WRAPPERS.values() for name in contract.body_functions}
    filters: dict[str, set[str]] = {name: set() for name in names}
    flow: dict[str, set[str]] = {name: set() for name in names}
    for node in ast.walk(tree):
        scope = _wrapper_scope(node, parents, names)
        if scope is None or not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg in QUERY_ARGUMENTS:
                filters[scope.name].update(_literal_dict_keys(keyword.value))
                flow[scope.name].update(_query_flow_names(keyword.value))
    for node in ast.walk(tree):
        scope = _wrapper_scope(node, parents, names)
        if scope is not None:
            filters[scope.name].update(_mutation_keys(node, flow[scope.name]))
    return filters


def _values(
    tree: ast.AST,
    parents: dict[ast.AST, ast.AST],
    scope: ast.AST | None,
    base: tuple[dict[str, str], dict[str, set[str] | None]],
    returns: dict[str, ast.AST],
) -> tuple[dict[str, str], dict[str, set[str] | None]]:
    strings, dictionaries = dict(base[0]), dict(base[1])
    for node in sorted(ast.walk(scope or tree), key=lambda item: getattr(item, "lineno", 0)):
        if (
            not isinstance(node, (ast.Assign, ast.AnnAssign))
            or _scope(node, parents) is not scope
            or node.value is None
        ):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                key = _string(target.slice, strings, returns)
                if key is not None:
                    current = dictionaries.get(target.value.id)
                    dictionaries[target.value.id] = set(current or ()) | {key}
                continue
            if not isinstance(target, ast.Name):
                continue
            if (value := _string(node.value, strings, returns)) is not None:
                strings[target.id] = value
            if isinstance(node.value, (ast.Dict, ast.Name)):
                dictionaries[target.id] = _dict_keys(node.value, dictionaries)
    return strings, dictionaries


def _path(call: ast.Call, strings: dict[str, str], returns: dict[str, ast.AST]) -> str | None:
    nodes = [keyword.value for keyword in call.keywords if keyword.arg in {"path", "url"}]
    function = _name(call.func)
    nodes += list(call.args if function in POSITIONAL_PATH_HELPERS else call.args[1:2])
    for node in nodes:
        if (value := _string(node, strings, returns)) is not None and PREFIX in value:
            return value[value.index(PREFIX) :]
    return None


def _queries(call: ast.Call, dictionaries: dict[str, set[str] | None]) -> tuple[set[str], bool]:
    keys: set[str] = set()
    unresolved = False
    for keyword in call.keywords:
        if keyword.arg not in QUERY_ARGUMENTS:
            continue
        if isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
            continue
        value = _dict_keys(keyword.value, dictionaries)
        if value is None:
            unresolved = True
        else:
            keys.update(value)
    return keys, unresolved


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    return next((item.value for item in call.keywords if item.arg == name), None)


def _string_map(node: ast.AST | None, strings: dict[str, str]) -> dict[str, str] | None:
    if node is None or not isinstance(node, ast.Dict):
        return None
    result: dict[str, str] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            return None
        mapped = _string(value, strings, {})
        if mapped is None:
            return None
        result[key.value] = mapped
    return result


def _string_list(node: ast.AST | None) -> tuple[str, ...] | None:
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    values = tuple(
        item.value
        for item in node.elts
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    )
    return values if len(values) == len(node.elts) else None


def _bulk_lookup_filters(call: ast.Call, strings: dict[str, str]) -> set[str]:
    if _name(call.func) != "rest_bulk_reconcile_async":
        return set()
    fallback = _keyword(call, "fallback_to_individual")
    if isinstance(fallback, ast.Constant) and fallback.value is False:
        return set()
    fields = _string_list(_keyword(call, "lookup_fields")) or ()
    mapping = _string_map(_keyword(call, "lookup_query_field_map"), strings) or {}
    return {mapping.get(field, field) for field in fields}


def _wrapper_filters(
    call: ast.Call,
    contract: WrapperContract,
    values: tuple[dict[str, str], dict[str, set[str] | None]],
    body_filters: dict[str, set[str]],
) -> tuple[set[str], bool]:
    strings, dictionaries = values
    filters, unresolved = _queries(call, dictionaries)
    filters.update(contract.implicit_filters)
    for function in contract.body_functions:
        filters.update(body_filters.get(function, set()))
    if contract.remap_lookup:
        filters.update(_remapped_lookup_filters(call, strings, dictionaries))
    if contract.parent_filter:
        parent = _keyword(call, "parent_field")
        parent_name = _string(parent, strings, {}) if parent is not None else None
        if parent_name is None:
            unresolved = True
        else:
            filters.update({parent_name, f"{parent_name}_id"})
    return filters, unresolved


def _remapped_lookup_filters(
    call: ast.Call,
    strings: dict[str, str],
    dictionaries: dict[str, set[str] | None],
) -> set[str]:
    lookup = _keyword(call, "lookup")
    lookup_keys = _dict_keys(lookup, dictionaries) if lookup is not None else None
    if lookup_keys is None:
        return set()
    mapping = _string_map(_keyword(call, "lookup_query_field_map"), strings) or {}
    return {mapping.get(key, key) for key in lookup_keys}


def _implicit(function: str, call: ast.Call) -> set[str]:
    keys: set[str] = set()
    if (function in {"rest_create_async", "rest_ensure_async"} or "reconcile" in function) and any(
        keyword.arg == "lookup" for keyword in call.keywords
    ):
        keys.add("limit")
    if function == "rest_list_paginated_async" or "reconcile" in function:
        keys.update({"limit", "offset"})
    return keys


def _helper_operations(function: str, call: ast.Call) -> tuple[tuple[str | None, bool], ...]:
    operations = HELPERS[function]
    if function == "rest_create_async" and _keyword(call, "lookup") is not None:
        return (("GET", False), *operations)
    fallback = _keyword(call, "fallback_to_individual")
    if (
        function == "rest_bulk_reconcile_async"
        and isinstance(fallback, ast.Constant)
        and fallback.value is False
    ):
        return tuple(operation for operation in operations if operation != ("PATCH", True))
    return operations


def _literal_lines(tree: ast.AST) -> set[int]:
    return {
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and PREFIX in node.value
    }


def _cover_call_literals(
    node: ast.Call,
    function: str,
    strings: dict[str, str],
    returns: dict[str, ast.AST],
    literals: set[int],
    covered: set[int],
) -> None:
    covered.update(
        item.lineno for item in ast.walk(node) if getattr(item, "lineno", -1) in literals
    )
    if function != "urlopen":
        return
    for returned in returns.values():
        if PREFIX in (_string(returned, strings, returns) or ""):
            covered.update(_literal_lines(returned))


def _append_operations(
    calls: list[PluginCall],
    *,
    relative: str,
    line: int,
    function: str,
    path: str,
    filters: set[str],
    operations: tuple[tuple[str | None, bool], ...],
) -> None:
    for method, detail in operations:
        target = f"{path.rstrip('/')}/{{dynamic}}/" if detail else path
        query_filters = filters if method in {None, "GET"} else ()
        calls.append(
            PluginCall(
                relative,
                line,
                function,
                target,
                method,
                tuple(sorted(query_filters)),
            )
        )


def _process_call(
    node: ast.Call,
    relative: str,
    values: tuple[dict[str, str], dict[str, set[str] | None]],
    returns: dict[str, ast.AST],
    literals: set[int],
    covered: set[int],
    calls: list[PluginCall],
    unresolved: list[UnresolvedCall],
    body_filters: dict[str, set[str]],
) -> None:
    strings, dictionaries = values
    function = _name(node.func)
    wrapper = WRAPPERS.get(function)
    value = _path(node, strings, returns)
    if value is None and wrapper is not None:
        value = wrapper.fixed_path
    if value is None:
        return
    _cover_call_literals(node, function, strings, returns, literals, covered)
    if function == "Request":
        return
    if function in PATH_ONLY_HELPERS:
        _append_operations(
            calls,
            relative=relative,
            line=node.lineno,
            function=function,
            path=value,
            filters=set(),
            operations=((None, False),),
        )
        return
    if function not in HELPERS and wrapper is None:
        unresolved.append(UnresolvedCall(relative, node.lineno, function, "unknown helper"))
        return
    if wrapper is None:
        filters, unknown_query = _queries(node, dictionaries)
        filters.update(_implicit(function, node))
        filters.update(_bulk_lookup_filters(node, strings))
        operations = _helper_operations(function, node)
    else:
        filters, unknown_query = _wrapper_filters(node, wrapper, values, body_filters)
        operations = wrapper.operations
    if unknown_query:
        unresolved.append(
            UnresolvedCall(relative, node.lineno, function, "unresolved query expression")
        )
    _append_operations(
        calls,
        relative=relative,
        line=node.lineno,
        function=function,
        path=value,
        filters=filters,
        operations=operations,
    )


def _process_wrapper_implementation(
    node: ast.Call,
    scope: ast.FunctionDef | ast.AsyncFunctionDef,
    relative: str,
    values: tuple[dict[str, str], dict[str, set[str] | None]],
    returns: dict[str, ast.AST],
    calls: list[PluginCall],
    body_filters: dict[str, set[str]],
) -> None:
    wrapper = WRAPPERS[scope.name]
    path = _path(node, values[0], returns) or wrapper.fixed_path
    if path is None:
        return
    _append_operations(
        calls,
        relative=relative,
        line=node.lineno,
        function=_name(node.func),
        path=path,
        filters=set(wrapper.implicit_filters) | body_filters.get(scope.name, set()),
        operations=wrapper.operations,
    )


def _e2e_cleanup_calls(tree: ast.AST, relative: str) -> list[PluginCall]:
    if relative != "proxbox_api/e2e/session.py":
        return []
    calls: list[PluginCall] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.List):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "object_types" for target in node.targets
        ):
            continue
        for item in node.value.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                continue
            if not item.value.startswith(PREFIX):
                continue
            calls.extend(
                (
                    PluginCall(
                        relative, item.lineno, "cleanup_e2e_objects", item.value, "GET", ("limit",)
                    ),
                    PluginCall(
                        relative,
                        item.lineno,
                        "cleanup_e2e_objects",
                        f"{item.value.rstrip('/')}/{{dynamic}}/",
                        "DELETE",
                        (),
                    ),
                )
            )
    return calls


def _extract_file(
    path: Path, source_root: Path
) -> tuple[list[PluginCall], list[UnresolvedCall], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    body_filters = _wrapper_body_filters(tree, parents)
    returns = _returns(tree)
    module = _values(tree, parents, None, ({}, {}), returns)
    scopes: dict[ast.AST | None, tuple[dict[str, str], dict[str, set[str] | None]]] = {None: module}
    relative = path.relative_to(source_root.parent).as_posix()
    calls: list[PluginCall] = []
    unresolved: list[UnresolvedCall] = []
    covered: set[int] = set()
    literals = _literal_lines(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        scope = _scope(node, parents)
        if (
            isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
            and scope.name in WRAPPERS
            and _name(node.func) in HELPERS
        ):
            if scope not in scopes:
                scopes[scope] = _values(tree, parents, scope, module, returns)
            _process_wrapper_implementation(
                node,
                scope,
                relative,
                scopes[scope],
                returns,
                calls,
                body_filters,
            )
            continue
        if scope not in scopes:
            scopes[scope] = _values(tree, parents, scope, module, returns)
        _process_call(
            node,
            relative,
            scopes[scope],
            returns,
            literals,
            covered,
            calls,
            unresolved,
            body_filters,
        )
    calls.extend(_e2e_cleanup_calls(tree, relative))
    missing = {f"{relative}:{line}" for line in literals - covered}
    return calls, unresolved, missing


def extract_plugin_calls(
    source_root: Path,
) -> tuple[list[PluginCall], list[UnresolvedCall], set[str]]:
    calls: list[PluginCall] = []
    unresolved: list[UnresolvedCall] = []
    missing: set[str] = set()
    for path in sorted(source_root.rglob("*.py")):
        new_calls, new_unresolved, new_missing = _extract_file(path, source_root)
        calls.extend(new_calls)
        unresolved.extend(new_unresolved)
        missing.update(new_missing)
    return sorted(set(calls)), sorted(set(unresolved)), missing


def _load_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        pytest.fail(f"plugin contract fixture is missing or malformed: {error}")
    if not isinstance(fixture, dict) or not isinstance(fixture.get("paths"), dict):
        pytest.fail("plugin contract fixture must contain a paths object")
    metadata = fixture.get("metadata")
    if not isinstance(metadata, dict):
        pytest.fail("plugin contract fixture must contain metadata")
    for key, expected in EXPECTED_METADATA.items():
        if metadata.get(key) != expected:
            pytest.fail(f"plugin contract fixture metadata {key!r} is not {expected!r}")
    return fixture


def _served(path: str, paths: dict[str, Any]) -> str | None:
    normalized = path.replace("{dynamic}", "1")
    matches = []
    for candidate in paths:
        pattern = "".join(
            r"[^/]+" if part.startswith("{") else re.escape(part)
            for part in re.split(r"(\{[^}]+\})", candidate)
        )
        if re.fullmatch(pattern, normalized):
            matches.append(candidate)
    return max(matches, key=len) if matches else None


def _contract_failures(calls: list[PluginCall], paths: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for call in calls:
        served = _served(call.path, paths)
        location = f"{call.file}:{call.line}"
        if served is None:
            failures.append(f"{location}: plugin route is not served: {call.path}")
            continue
        if call.method is not None and call.method not in paths[served]:
            failures.append(f"{location}: {served} does not serve {call.method}")
            continue
        unknown = sorted(
            filter_name
            for filter_name in set(call.filters) - set(paths[served].get("GET", []))
            if (served, filter_name) not in FORWARD_COMPATIBLE_FILTERS
        )
        if unknown:
            failures.append(f"{location}: {served} rejects query keys {unknown}")
    return failures


def _operation_keys(calls: list[PluginCall]) -> set[OperationKey]:
    return {(call.file, call.function, call.path, call.method) for call in calls}


def _literal_failures(calls: list[PluginCall], missing: set[str]) -> list[str]:
    allowed = set(NON_EXECUTABLE_LITERAL_ALLOWLIST) | set(ROUTE_CONSTANT_BINDINGS)
    failures = [f"unaccounted plugin-path literal: {key}" for key in sorted(missing - allowed)]
    failures.extend(
        f"stale plugin-path literal allowlist entry: {key}" for key in sorted(allowed - missing)
    )
    operations = _operation_keys(calls)
    for key, required in ROUTE_CONSTANT_BINDINGS.items():
        absent = sorted(set(required) - operations)
        if absent:
            failures.append(f"route constant {key} has no bound checked operation: {absent}")
    return failures


def test_backend_plugin_rest_contract() -> None:
    calls, unresolved, missing = extract_plugin_calls(ROOT / "proxbox_api")
    keys = {item.key for item in unresolved}
    assert not [item for item in unresolved if item.key not in DYNAMIC_PATH_ALLOWLIST], (
        f"unresolved plugin REST call sites: {unresolved}"
    )
    assert not sorted(set(DYNAMIC_PATH_ALLOWLIST) - keys), "stale dynamic-path allowlist entries"
    literal_failures = _literal_failures(calls, missing)
    assert not literal_failures, "\n" + "\n".join(literal_failures)
    failures = _contract_failures(calls, _load_fixture()["paths"])
    assert not failures, "\n" + "\n".join(failures)


def test_extractor_preserves_head_coverage_floor() -> None:
    calls, _, _ = extract_plugin_calls(ROOT / "proxbox_api")
    routes = {call.path for call in calls}
    route_filters = {(call.path, key) for call in calls for key in call.filters}
    floor_filters = {
        (route, key) for route, filters in HEAD_ROUTE_FILTERS.items() for key in filters
    }
    assert routes >= set(HEAD_ROUTE_FILTERS)
    assert route_filters >= floor_filters
    assert {
        ("/api/plugins/proxbox/vm-cloudinit/", "virtual_machine_id"),
        ("/api/plugins/proxbox/proxmox-nodes/", "proxmox_cluster_id"),
        ("/api/plugins/proxbox/sdn-route-maps/", "order"),
        ("/api/plugins/proxbox/sdn-prefix-lists/", "cidr"),
    } <= floor_filters
    assert "/api/plugins/proxbox/nodes/" in HEAD_ROUTE_FILTERS


def test_extractor_preserves_call_site_floor() -> None:
    calls, _, _ = extract_plugin_calls(ROOT / "proxbox_api")
    current = {
        (call.file, call.function, call.path, call.method, filter_name)
        for call in calls
        for filter_name in (call.filters or (None,))
    }
    missing = sorted(CALL_SITE_FLOOR - current, key=repr)
    assert not missing, f"call-site contract coverage regressed: {missing}"


def test_wrapper_callers_emit_read_create_and_detail_patch_contracts() -> None:
    calls, _, _ = extract_plugin_calls(ROOT / "proxbox_api")
    operations = {(call.path, call.method) for call in calls}
    for route in (
        "/api/plugins/proxbox/guest-vm-interfaces/",
        "/api/plugins/proxbox/sdn-route-maps/",
        "/api/plugins/proxbox/sync-state/virtual-machines/",
    ):
        assert (route, "GET") in operations
        assert (route, "POST") in operations
        assert (f"{route}{{dynamic}}/", "PATCH") in operations


@pytest.mark.parametrize(
    "removed_path",
    [
        "/api/plugins/proxbox/snapshots/",
        "/api/plugins/proxbox/snapshots/{id}/",
    ],
)
def test_snapshot_delete_requires_collection_and_detail_delete(removed_path: str) -> None:
    paths = copy.deepcopy(_load_fixture()["paths"])
    paths[removed_path].pop("DELETE")
    calls, _, _ = extract_plugin_calls(ROOT / "proxbox_api")
    snapshot_deletes = [
        call
        for call in calls
        if call.function == "rest_bulk_delete_async"
        and call.path.startswith("/api/plugins/proxbox/snapshots/")
    ]
    assert snapshot_deletes
    assert any(
        "does not serve DELETE" in failure
        for failure in _contract_failures(snapshot_deletes, paths)
    )


def test_create_with_lookup_emits_recovery_get(tmp_path: Path) -> None:
    calls, unresolved, _ = extract_plugin_calls(
        _source(
            tmp_path,
            "async def f(nb):\n"
            " return await rest_create_async(\n"
            '  nb,"/api/plugins/proxbox/nodes/",{},lookup={"name":"n"}\n'
            " )\n",
        )
    )
    assert not unresolved
    assert {(call.method, call.filters) for call in calls} == {
        ("GET", ("limit", "name")),
        ("POST", ()),
    }


def test_executable_literal_transports_are_checked() -> None:
    calls, _, _ = extract_plugin_calls(ROOT / "proxbox_api")
    operations = _operation_keys(calls)
    for required in (
        ROUTE_CONSTANT_BINDINGS["proxbox_api/plugin_key_authority.py:20"],
        ROUTE_CONSTANT_BINDINGS["proxbox_api/e2e/session.py:206"],
    ):
        assert set(required) <= operations


@pytest.mark.parametrize(
    ("relative", "original", "replacement"),
    [
        (
            "plugin_key_authority.py",
            "/api/plugins/proxbox/settings/runtime/",
            "/api/plugins/proxbox/settings/removed-action/",
        ),
        (
            "e2e/session.py",
            "/api/plugins/proxbox/backups/",
            "/api/plugins/proxbox/backups/removed-action/",
        ),
    ],
)
def test_executable_literal_path_drift_fails(
    tmp_path: Path, relative: str, original: str, replacement: str
) -> None:
    source = (ROOT / "proxbox_api" / relative).read_text(encoding="utf-8")
    package = _source_file(tmp_path, relative, source.replace(original, replacement, 1))
    calls, _, missing = extract_plugin_calls(package)
    failures = [
        *_contract_failures(calls, _load_fixture()["paths"]),
        *_literal_failures(calls, missing),
    ]
    assert any("no bound checked operation" in failure for failure in failures)


@pytest.mark.parametrize(
    ("source", "function"),
    [
        (
            "async def _best_effort_reconcile(nb,path,*,lookup,payload):\n"
            " return await rest_first_async(\n"
            '  nb,path,query={**lookup,"missing_best_effort":1}\n'
            " )\n"
            "async def run(nb):\n"
            " return await _best_effort_reconcile(\n"
            '  nb,"/api/plugins/proxbox/nodes/",lookup={"name":"n"},payload={}\n'
            " )\n",
            "_best_effort_reconcile",
        ),
        (
            "async def _reconcile(nb,path,*,lookup,payload):\n"
            " return await rest_reconcile_async_with_status(\n"
            '  nb,path,lookup={**lookup,"missing_sdn":1},payload=payload\n'
            " )\n"
            "async def _plugin_upsert_counted(nb,path,*,lookup,payload):\n"
            " return await _reconcile(nb,path,lookup=lookup,payload=payload)\n"
            "async def run(nb):\n"
            " return await _plugin_upsert_counted(\n"
            '  nb,"/api/plugins/proxbox/nodes/",lookup={"name":"n"},payload={}\n'
            " )\n",
            "_plugin_upsert_counted",
        ),
        (
            "async def _lookup_parent_sidecar(nb,*,path,parent_field,parent_id):\n"
            " return await rest_first_async(\n"
            '  nb,path,query={f"{parent_field}_id":parent_id,"missing_sidecar":1}\n'
            " )\n"
            "async def _upsert_parent_sidecar(nb,*,path,parent_field,parent_id,payload):\n"
            " return await _lookup_parent_sidecar(\n"
            "  nb,path=path,parent_field=parent_field,parent_id=parent_id\n"
            " )\n"
            "async def run(nb):\n"
            " return await _upsert_parent_sidecar(\n"
            '  nb,path="/api/plugins/proxbox/sync-state/devices/",\n'
            '  parent_field="device",parent_id=1,payload={}\n'
            " )\n",
            "_upsert_parent_sidecar",
        ),
    ],
)
def test_wrapper_body_literal_filter_mutation_fails(
    tmp_path: Path, source: str, function: str
) -> None:
    calls, unresolved, _ = extract_plugin_calls(_source(tmp_path, source))
    assert not unresolved
    wrapper_calls = [call for call in calls if call.function == function]
    failures = _contract_failures(wrapper_calls, _load_fixture()["paths"])
    assert any("rejects query keys ['missing_" in failure for failure in failures)


def _source(root: Path, source: str) -> Path:
    package = root / "proxbox_api"
    package.mkdir(parents=True)
    (package / "sample.py").write_text(source, encoding="utf-8")
    return package


def _source_file(root: Path, relative: str, source: str) -> Path:
    package = root / "proxbox_api"
    path = package / relative
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    return package


def test_unknown_helper_with_local_path_fails_closed(tmp_path: Path) -> None:
    _, unresolved, _ = extract_plugin_calls(
        _source(
            tmp_path,
            'async def f(nb):\n p="/api/plugins/proxbox/nodes/"\n return await _fetch_rows(nb,p)\n',
        )
    )
    assert unresolved[0].reason == "unknown helper"


@pytest.mark.parametrize("query", ["query=filters", "query={**unknown}"])
def test_unresolved_query_fails_closed(tmp_path: Path, query: str) -> None:
    _, unresolved, _ = extract_plugin_calls(
        _source(
            tmp_path,
            f'async def f(nb):\n return await rest_list_async(nb,"/api/plugins/proxbox/nodes/",{query})\n',
        )
    )
    assert unresolved[0].reason == "unresolved query expression"


def test_resolved_dict_unpacking(tmp_path: Path) -> None:
    calls, unresolved, _ = extract_plugin_calls(
        _source(
            tmp_path,
            'Q={"name":1}\nasync def f(nb):\n return await rest_list_async(nb,"/api/plugins/proxbox/nodes/",query={**Q})\n',
        )
    )
    assert not unresolved and calls[0].filters == ("name",)


def test_credential_transports_are_gets() -> None:
    calls, _, _ = extract_plugin_calls(ROOT / "proxbox_api")
    paths = {(item.file, item.path, item.method) for item in calls if item.function == "urlopen"}
    assert (
        "proxbox_api/services/ssh_terminal.py",
        "/api/plugins/proxbox/ssh-credentials/by-endpoint/{dynamic}/credentials/",
        "GET",
    ) in paths
    assert (
        "proxbox_api/services/hardware_discovery.py",
        "/api/plugins/proxbox/ssh-credentials/by-node/{dynamic}/credentials/",
        "GET",
    ) in paths


def test_patch_requires_detail_path() -> None:
    call = PluginCall(
        "x", 1, "rest_patch_async", "/api/plugins/proxbox/nodes/{dynamic}/", "PATCH", ()
    )
    assert (
        "not served"
        in _contract_failures([call], {"/api/plugins/proxbox/nodes/": {"PATCH": []}})[0]
    )


@pytest.mark.parametrize(
    ("path", "parent_field"),
    [
        ("/api/plugins/proxbox/sync-state/virtual-machines/", "virtual_machine"),
        ("/api/plugins/proxbox/sync-state/devices/", "device"),
        ("/api/plugins/proxbox/sync-state/clusters/", "cluster"),
        ("/api/plugins/proxbox/sync-state/virtual-disks/", "virtual_disk"),
        ("/api/plugins/proxbox/sync-state/vm-interfaces/", "vm_interface"),
    ],
)
def test_sync_state_parent_filter_is_served(path: str, parent_field: str) -> None:
    parameters = _load_fixture()["paths"][path]["GET"]
    assert f"{parent_field}_id" in parameters


def _generator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "generator", ROOT / "scripts/generate_plugin_contract_fixture.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _arguments(path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        openapi=path,
        plugin_version="x",
        plugin_wheel_sha256="x",
        image="x",
        image_digest="x",
        netbox_version="x",
        generation_date="x",
        generation_command="x",
    )


def test_generator_resolves_parameter_refs(tmp_path: Path) -> None:
    document = {
        "components": {
            "parameters": {"A": {"name": "a", "in": "query"}, "B": {"name": "b", "in": "query"}}
        },
        "paths": {
            "/api/plugins/proxbox/nodes/": {
                "parameters": [{"$ref": "#/components/parameters/A"}],
                "get": {"parameters": [{"$ref": "#/components/parameters/B"}]},
            }
        },
    }
    path = tmp_path / "x.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert _generator().generate_fixture(_arguments(path))["paths"]["/api/plugins/proxbox/nodes/"][
        "GET"
    ] == ["a", "b"]


def test_generator_rejects_missing_ref(tmp_path: Path) -> None:
    document = {
        "paths": {
            "/api/plugins/proxbox/nodes/": {
                "get": {"parameters": [{"$ref": "#/components/parameters/X"}]}
            }
        }
    }
    path = tmp_path / "x.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="cannot resolve"):
        _generator().generate_fixture(_arguments(path))


@pytest.mark.parametrize("contents", ["", "{}", "not json"])
def test_fixture_fails_closed(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "fixture"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(pytest.fail.Exception):
        _load_fixture(path)


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ('query["added_key"] = 1', {"added_key"}),
        ('query.update({"updated_key": 1})', {"updated_key"}),
        ("query[str(path)] = 1", {UNRESOLVED_WRAPPER_KEY}),
        ("query |= extra", {UNRESOLVED_WRAPPER_KEY}),
    ],
)
def test_wrapper_body_local_query_mutations_are_checked(mutation: str, expected: set[str]) -> None:
    source = (
        "async def _best_effort_reconcile(nb, path, *, lookup, extra):\n"
        "    query = {key: value for key, value in lookup.items()}\n"
        "    payload = {}\n"
        '    payload["not_a_query_key"] = 1\n'
        f"    {mutation}\n"
        '    return await rest_first_async(nb, path, query={**query, "limit": 2})\n'
    )
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    filters = _wrapper_body_filters(tree, parents)["_best_effort_reconcile"]
    assert expected <= filters
    assert "not_a_query_key" not in filters

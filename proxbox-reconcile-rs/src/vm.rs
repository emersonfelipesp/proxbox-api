use std::collections::HashMap;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use thiserror::Error;

use crate::diff::{apply_overwrite_rules, diff_payloads};
use crate::normalize::{
    normalize_current_vm_payload, normalize_desired_vm_payload, normalize_proxmox_vm_type,
    relation_id,
};

#[derive(Debug, Error)]
pub enum ReconcileError {
    #[error("invalid input JSON: {0}")]
    InvalidInput(#[from] serde_json::Error),
}

#[derive(Debug, Deserialize)]
pub struct VmQueueInput {
    pub prepared_vms: Vec<PreparedVm>,
    pub netbox_snapshot: Vec<Value>,
    pub flags: VmFlags,
}

#[derive(Debug, Deserialize)]
pub struct PreparedVm {
    pub cluster_name: String,
    pub resource: Value,
    pub desired_payload: Map<String, Value>,
    #[allow(dead_code)]
    pub lookup: Map<String, Value>,
    pub vm_type: String,
}

#[derive(Debug, Deserialize)]
pub struct VmFlags {
    pub overwrite_vm_role: bool,
    pub overwrite_vm_type: bool,
    pub overwrite_vm_tags: bool,
    pub overwrite_vm_description: bool,
    pub overwrite_vm_custom_fields: bool,
    pub supports_virtual_machine_type_field: bool,
}

#[derive(Debug, Serialize)]
pub struct VmOperation {
    pub method: String,
    pub cluster_name: String,
    pub vmid: i64,
    pub vm_type: String,
    pub desired_payload: Map<String, Value>,
    pub existing_record: Option<Value>,
    pub patch_payload: Map<String, Value>,
}

type TypedSnapshotIndex = HashMap<(i64, i64, String), Value>;
type UntypedSnapshotIndex = HashMap<(i64, i64), Vec<Value>>;

pub fn build_vm_operation_queue_json(input: &[u8]) -> Result<Vec<u8>, ReconcileError> {
    let input: VmQueueInput = serde_json::from_slice(input)?;
    let operations = build_vm_operation_queue(input);
    Ok(serde_json::to_vec(&operations)?)
}

fn build_vm_operation_queue(input: VmQueueInput) -> Vec<VmOperation> {
    let (typed_index, untyped_candidates) =
        build_vm_snapshot_identity_indexes(input.netbox_snapshot);
    let mut operations = Vec::with_capacity(input.prepared_vms.len());

    for prepared in input.prepared_vms {
        let cluster_id = relation_id(prepared.desired_payload.get("cluster"));
        let proxmox_vmid = relation_id(prepared.resource.get("vmid"));
        let Some(vmid) = proxmox_vmid else {
            operations.push(create_op(prepared, 0, &input.flags));
            continue;
        };
        let Some(cluster_id) = cluster_id else {
            operations.push(create_op(prepared, vmid, &input.flags));
            continue;
        };

        let Some(existing_record) = select_existing_vm_record(
            &prepared,
            cluster_id,
            vmid,
            &typed_index,
            &untyped_candidates,
        ) else {
            operations.push(create_op(prepared, vmid, &input.flags));
            continue;
        };

        let desired_for_diff = normalize_desired_vm_payload(
            &prepared.desired_payload,
            input.flags.supports_virtual_machine_type_field,
        );
        let current_for_diff = normalize_current_vm_payload(
            &existing_record,
            input.flags.supports_virtual_machine_type_field,
        );
        let mut patch_payload = diff_payloads(&desired_for_diff, &current_for_diff);
        apply_overwrite_rules(
            &mut patch_payload,
            &existing_record,
            &current_for_diff,
            &desired_for_diff,
            &input.flags,
        );

        let method = if patch_payload.is_empty() {
            "GET"
        } else {
            "UPDATE"
        };

        operations.push(VmOperation {
            method: method.to_string(),
            cluster_name: prepared.cluster_name,
            vmid,
            vm_type: prepared.vm_type,
            desired_payload: output_desired_payload(prepared.desired_payload, &input.flags),
            existing_record: Some(existing_record),
            patch_payload,
        });
    }

    operations
}

fn create_op(prepared: PreparedVm, vmid: i64, flags: &VmFlags) -> VmOperation {
    VmOperation {
        method: "CREATE".to_string(),
        cluster_name: prepared.cluster_name,
        vmid,
        vm_type: prepared.vm_type,
        desired_payload: output_desired_payload(prepared.desired_payload, flags),
        existing_record: None,
        patch_payload: Map::new(),
    }
}

fn output_desired_payload(
    mut desired_payload: Map<String, Value>,
    flags: &VmFlags,
) -> Map<String, Value> {
    if !flags.supports_virtual_machine_type_field {
        desired_payload.remove("virtual_machine_type");
    }
    desired_payload
}

fn build_vm_snapshot_identity_indexes(
    netbox_snapshot: Vec<Value>,
) -> (TypedSnapshotIndex, UntypedSnapshotIndex) {
    let mut typed_index = HashMap::new();
    let mut untyped_candidates: UntypedSnapshotIndex = HashMap::new();

    for record in netbox_snapshot {
        let Some((cluster_id, proxmox_vmid)) = extract_cluster_and_proxmox_vmid(&record) else {
            continue;
        };
        untyped_candidates
            .entry((cluster_id, proxmox_vmid))
            .or_default()
            .push(record.clone());
        if let Some(vm_type) = extract_proxmox_vm_type(&record) {
            typed_index
                .entry((cluster_id, proxmox_vmid, vm_type))
                .or_insert(record);
        }
    }

    (typed_index, untyped_candidates)
}

fn select_existing_vm_record(
    prepared: &PreparedVm,
    cluster_id: i64,
    proxmox_vmid: i64,
    typed_index: &TypedSnapshotIndex,
    untyped_candidates: &UntypedSnapshotIndex,
) -> Option<Value> {
    let prepared_vm_type =
        normalize_proxmox_vm_type(Some(&Value::String(prepared.vm_type.clone())));
    let untyped_key = (cluster_id, proxmox_vmid);

    if let Some(prepared_vm_type) = prepared_vm_type {
        if let Some(record) = typed_index.get(&(cluster_id, proxmox_vmid, prepared_vm_type)) {
            return Some(record.clone());
        }
        let candidates = untyped_candidates.get(&untyped_key)?;
        if candidates.len() == 1 && extract_proxmox_vm_type(&candidates[0]).is_none() {
            return Some(candidates[0].clone());
        }
        return None;
    }

    let candidates = untyped_candidates.get(&untyped_key)?;
    (candidates.len() == 1).then(|| candidates[0].clone())
}

fn extract_cluster_and_proxmox_vmid(record: &Value) -> Option<(i64, i64)> {
    let object = record.as_object()?;
    let cluster_id = relation_id(object.get("cluster"))?;
    let custom_fields = object.get("custom_fields")?.as_object()?;
    let raw_vmid = custom_fields.get("proxmox_vm_id")?;
    let proxmox_vmid = relation_id(Some(raw_vmid))?;
    Some((cluster_id, proxmox_vmid))
}

fn extract_proxmox_vm_type(record: &Value) -> Option<String> {
    let custom_fields = record.get("custom_fields")?.as_object()?;
    normalize_proxmox_vm_type(custom_fields.get("proxmox_vm_type"))
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn default_flags() -> Value {
        json!({
            "overwrite_vm_role": true,
            "overwrite_vm_type": true,
            "overwrite_vm_tags": true,
            "overwrite_vm_description": true,
            "overwrite_vm_custom_fields": true,
            "supports_virtual_machine_type_field": true
        })
    }

    fn prepared(vmid: i64, vm_type: &str) -> Value {
        json!({
            "cluster_name": "cluster-a",
            "resource": {"name": format!("{vm_type}-{vmid}"), "vmid": vmid, "type": vm_type},
            "desired_payload": {
                "name": format!("{vm_type}-{vmid}"),
                "status": "active",
                "cluster": 1,
                "device": 10,
                "role": 20,
                "vcpus": 2,
                "memory": 2048,
                "disk": 30,
                "tags": [99],
                "custom_fields": {"proxmox_vm_id": vmid, "proxmox_vm_type": vm_type},
                "description": "Synced from Proxmox node pve01"
            },
            "lookup": {"cf_proxmox_vm_id": vmid, "cluster_id": 1},
            "vm_type": vm_type
        })
    }

    fn snapshot(record_id: i64, vmid: i64, vm_type: Option<&str>) -> Value {
        let mut custom_fields = Map::new();
        custom_fields.insert("proxmox_vm_id".to_string(), json!(vmid));
        if let Some(vm_type) = vm_type {
            custom_fields.insert("proxmox_vm_type".to_string(), json!(vm_type));
        }
        json!({
            "id": record_id,
            "name": format!("{}-{vmid}", vm_type.unwrap_or("qemu")),
            "status": "active",
            "cluster": {"id": 1},
            "device": {"id": 10},
            "role": {"id": 20},
            "vcpus": 2,
            "memory": 2048,
            "disk": 30,
            "tags": [{"id": 99}],
            "custom_fields": custom_fields,
            "description": "Synced from Proxmox node pve01"
        })
    }

    fn run(input: Value) -> Vec<Value> {
        let output = build_vm_operation_queue_json(input.to_string().as_bytes()).unwrap();
        serde_json::from_slice(&output).unwrap()
    }

    #[test]
    fn classifies_create_get_update() {
        let input = json!({
            "prepared_vms": [prepared(100, "qemu"), prepared(101, "qemu"), prepared(102, "qemu")],
            "netbox_snapshot": [
                snapshot(2101, 101, Some("qemu")),
                {
                    "id": 2102,
                    "name": "qemu-102",
                    "status": "active",
                    "cluster": {"id": 1},
                    "device": {"id": 10},
                    "role": {"id": 20},
                    "vcpus": 2,
                    "memory": 1024,
                    "disk": 30,
                    "tags": [{"id": 99}],
                    "custom_fields": {"proxmox_vm_id": 102, "proxmox_vm_type": "qemu"},
                    "description": "Synced from Proxmox node pve01"
                }
            ],
            "flags": default_flags()
        });

        let operations = run(input);

        assert_eq!(operations[0]["method"], "CREATE");
        assert_eq!(operations[1]["method"], "GET");
        assert_eq!(operations[2]["method"], "UPDATE");
        assert_eq!(operations[2]["patch_payload"]["memory"], 2048);
    }

    #[test]
    fn keeps_qemu_and_lxc_same_vmid_separate() {
        let input = json!({
            "prepared_vms": [prepared(100, "qemu"), prepared(100, "lxc")],
            "netbox_snapshot": [snapshot(3001, 100, Some("qemu")), snapshot(3002, 100, Some("lxc"))],
            "flags": default_flags()
        });

        let operations = run(input);

        assert_eq!(operations[0]["method"], "GET");
        assert_eq!(operations[0]["existing_record"]["id"], 3001);
        assert_eq!(operations[1]["method"], "GET");
        assert_eq!(operations[1]["existing_record"]["id"], 3002);
    }

    #[test]
    fn ambiguous_untyped_record_creates_instead_of_guessing() {
        let input = json!({
            "prepared_vms": [prepared(116, "qemu")],
            "netbox_snapshot": [snapshot(2116, 116, None), snapshot(2117, 116, Some("lxc"))],
            "flags": default_flags()
        });

        let operations = run(input);

        assert_eq!(operations[0]["method"], "CREATE");
    }

    #[test]
    fn tags_are_order_independent_and_merged_when_needed() {
        let mut current = snapshot(2112, 112, Some("qemu"));
        current["tags"] = json!([{"id": 3}, 1, 2]);
        let mut desired = prepared(112, "qemu");
        desired["desired_payload"]["tags"] = json!([1, 2, 3]);
        let input = json!({
            "prepared_vms": [desired],
            "netbox_snapshot": [current],
            "flags": default_flags()
        });

        let operations = run(input);

        assert_eq!(operations[0]["method"], "GET");
    }

    #[test]
    fn custom_field_null_and_missing_are_different() {
        let mut desired = prepared(113, "qemu");
        desired["desired_payload"]["custom_fields"] = json!({"proxmox_vm_id": 113, "foo": null});
        let mut current = snapshot(2113, 113, None);
        current["custom_fields"] = json!({"proxmox_vm_id": 113});
        current["name"] = json!("qemu-113");
        let input = json!({
            "prepared_vms": [desired],
            "netbox_snapshot": [current],
            "flags": default_flags()
        });

        let operations = run(input);

        assert_eq!(operations[0]["method"], "UPDATE");
        assert_eq!(
            operations[0]["patch_payload"]["custom_fields"],
            json!({"proxmox_vm_id": 113, "foo": null})
        );
    }

    #[test]
    fn invalid_json_returns_error() {
        assert!(build_vm_operation_queue_json(b"not-json").is_err());
    }
}

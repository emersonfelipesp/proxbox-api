use std::collections::BTreeSet;

use serde_json::{Map, Number, Value};

use crate::normalize::{normalize_tags, number_value, relation_id};
use crate::vm::VmFlags;

pub fn diff_payloads(
    desired: &Map<String, Value>,
    current: &Map<String, Value>,
) -> Map<String, Value> {
    desired
        .iter()
        .filter_map(|(field_name, desired_value)| {
            let differs = current
                .get(field_name)
                .map(|current_value| !json_eq_loose(desired_value, current_value))
                .unwrap_or(true);
            differs.then(|| (field_name.clone(), desired_value.clone()))
        })
        .collect()
}

pub fn apply_overwrite_rules(
    patch: &mut Map<String, Value>,
    existing_record: &Value,
    current: &Map<String, Value>,
    desired: &Map<String, Value>,
    flags: &VmFlags,
) {
    if !flags.overwrite_vm_role && relation_id(existing_record.get("role")).is_some() {
        patch.remove("role");
    }
    if !flags.overwrite_vm_type
        && relation_id(existing_record.get("virtual_machine_type")).is_some()
    {
        patch.remove("virtual_machine_type");
    }
    if !flags.overwrite_vm_description
        && existing_record
            .get("description")
            .and_then(Value::as_str)
            .map(|description| !description.is_empty())
            .unwrap_or(false)
    {
        patch.remove("description");
    }
    if !flags.overwrite_vm_custom_fields
        && existing_record
            .get("custom_fields")
            .and_then(Value::as_object)
            .map(|custom_fields| !custom_fields.is_empty())
            .unwrap_or(false)
    {
        patch.remove("custom_fields");
    }

    if !flags.overwrite_vm_tags {
        if existing_record
            .get("tags")
            .and_then(Value::as_array)
            .map(|tags| !tags.is_empty())
            .unwrap_or(false)
        {
            patch.remove("tags");
        }
    } else if patch.contains_key("tags") {
        let existing_normalized = normalize_tags(current.get("tags"));
        let desired_normalized = normalize_tags(desired.get("tags"));
        let merged: Vec<i64> = existing_normalized
            .iter()
            .chain(desired_normalized.iter())
            .copied()
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        if merged == existing_normalized {
            patch.remove("tags");
        } else {
            patch.insert(
                "tags".to_string(),
                Value::Array(merged.into_iter().map(number_value).collect()),
            );
        }
    }
}

pub fn json_eq_loose(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => numbers_eq_loose(x, y),
        (Value::Array(left), Value::Array(right)) => {
            left.len() == right.len()
                && left
                    .iter()
                    .zip(right.iter())
                    .all(|(left, right)| json_eq_loose(left, right))
        }
        (Value::Object(left), Value::Object(right)) => {
            left.len() == right.len()
                && left.iter().all(|(key, left_value)| {
                    right
                        .get(key)
                        .is_some_and(|right_value| json_eq_loose(left_value, right_value))
                })
        }
        (Value::Null, Value::Null) => true,
        _ => a == b,
    }
}

fn numbers_eq_loose(left: &Number, right: &Number) -> bool {
    match (left.as_f64(), right.as_f64()) {
        (Some(left), Some(right)) => left == right,
        _ => left == right,
    }
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn loose_number_equality_matches_python_int_float_behavior() {
        assert!(json_eq_loose(&json!(2048), &json!(2048.0)));
        assert!(!json_eq_loose(&json!(2048), &json!(2049.0)));
    }
}

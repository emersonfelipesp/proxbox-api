use std::collections::BTreeSet;

use serde_json::{Map, Number, Value};

pub fn relation_id(value: Option<&Value>) -> Option<i64> {
    match value {
        Some(Value::Number(number)) => number_to_i64(number),
        Some(Value::String(text)) => parse_i64(text),
        Some(Value::Object(object)) => object
            .get("id")
            .and_then(|value| relation_id(Some(value)))
            .or_else(|| {
                object
                    .get("value")
                    .and_then(|value| relation_id(Some(value)))
            }),
        _ => None,
    }
}

pub fn normalize_current_vm_payload(record: &Value, supports_vm_type: bool) -> Map<String, Value> {
    let mut payload = Map::new();
    let object = record.as_object();

    insert_raw(
        &mut payload,
        "name",
        object.and_then(|item| item.get("name")),
    );
    payload.insert(
        "status".to_string(),
        Value::String(normalize_status(object.and_then(|item| item.get("status")))),
    );
    insert_relation(
        &mut payload,
        "cluster",
        object.and_then(|item| item.get("cluster")),
    );
    insert_relation(
        &mut payload,
        "device",
        object.and_then(|item| item.get("device")),
    );
    insert_relation(
        &mut payload,
        "site",
        object.and_then(|item| item.get("site")),
    );
    if supports_vm_type {
        insert_relation(
            &mut payload,
            "virtual_machine_type",
            object.and_then(|item| item.get("virtual_machine_type")),
        );
    }
    insert_relation(
        &mut payload,
        "role",
        object.and_then(|item| item.get("role")),
    );
    insert_vm_int(
        &mut payload,
        "vcpus",
        object.and_then(|item| item.get("vcpus")),
    );
    insert_vm_int(
        &mut payload,
        "memory",
        object.and_then(|item| item.get("memory")),
    );
    insert_vm_int(
        &mut payload,
        "disk",
        object.and_then(|item| item.get("disk")),
    );
    payload.insert(
        "tags".to_string(),
        Value::Array(
            normalize_tags(object.and_then(|item| item.get("tags")))
                .into_iter()
                .map(number_value)
                .collect(),
        ),
    );
    insert_custom_fields(
        &mut payload,
        object.and_then(|item| item.get("custom_fields")),
    );
    insert_optional_raw(
        &mut payload,
        "description",
        object.and_then(|item| item.get("description")),
    );

    payload
}

pub fn normalize_desired_vm_payload(
    desired_payload: &Map<String, Value>,
    supports_vm_type: bool,
) -> Map<String, Value> {
    let mut payload = Map::new();

    insert_raw(&mut payload, "name", desired_payload.get("name"));
    payload.insert(
        "status".to_string(),
        Value::String(normalize_status(desired_payload.get("status"))),
    );
    insert_relation(&mut payload, "cluster", desired_payload.get("cluster"));
    insert_relation(&mut payload, "device", desired_payload.get("device"));
    insert_relation(&mut payload, "site", desired_payload.get("site"));
    if supports_vm_type {
        insert_relation(
            &mut payload,
            "virtual_machine_type",
            desired_payload.get("virtual_machine_type"),
        );
    }
    insert_relation(&mut payload, "role", desired_payload.get("role"));
    insert_vm_int(&mut payload, "vcpus", desired_payload.get("vcpus"));
    insert_vm_int(&mut payload, "memory", desired_payload.get("memory"));
    insert_vm_int(&mut payload, "disk", desired_payload.get("disk"));
    payload.insert(
        "tags".to_string(),
        Value::Array(
            normalize_tags(desired_payload.get("tags"))
                .into_iter()
                .map(number_value)
                .collect(),
        ),
    );
    insert_custom_fields(&mut payload, desired_payload.get("custom_fields"));
    insert_optional_raw(
        &mut payload,
        "description",
        desired_payload.get("description"),
    );

    payload
}

pub fn normalize_tags(value: Option<&Value>) -> Vec<i64> {
    let mut normalized = BTreeSet::new();
    let Some(Value::Array(items)) = value else {
        return Vec::new();
    };

    for item in items {
        let candidate = match item {
            Value::Object(object) => object.get("id").unwrap_or(&Value::Null),
            other => other,
        };
        if let Some(tag_id) = value_to_i64(candidate) {
            normalized.insert(tag_id);
        }
    }

    normalized.into_iter().collect()
}

pub fn normalize_proxmox_vm_type(value: Option<&Value>) -> Option<String> {
    let mut candidate = value;
    if let Some(Value::Object(object)) = value {
        candidate = object
            .get("value")
            .or_else(|| object.get("slug"))
            .or_else(|| object.get("name"))
            .or_else(|| object.get("label"));
    }

    match candidate {
        Some(Value::String(text)) => {
            let normalized = text.trim().to_lowercase();
            (!normalized.is_empty()).then_some(normalized)
        }
        Some(Value::Number(number)) => Some(number.to_string().to_lowercase()),
        Some(Value::Bool(value)) => Some(value.to_string()),
        _ => None,
    }
}

pub fn number_value(value: i64) -> Value {
    Value::Number(Number::from(value))
}

fn insert_raw(payload: &mut Map<String, Value>, key: &str, value: Option<&Value>) {
    if let Some(value) = value {
        if !value.is_null() {
            payload.insert(key.to_string(), value.clone());
        }
    }
}

fn insert_optional_raw(payload: &mut Map<String, Value>, key: &str, value: Option<&Value>) {
    insert_raw(payload, key, value);
}

fn insert_relation(payload: &mut Map<String, Value>, key: &str, value: Option<&Value>) {
    if let Some(relation) = relation_id(value) {
        if relation > 0 {
            payload.insert(key.to_string(), number_value(relation));
        }
    }
}

fn insert_vm_int(payload: &mut Map<String, Value>, key: &str, value: Option<&Value>) {
    payload.insert(
        key.to_string(),
        number_value(value.and_then(value_to_i64).unwrap_or(0)),
    );
}

fn insert_custom_fields(payload: &mut Map<String, Value>, value: Option<&Value>) {
    match value {
        Some(Value::Object(object)) => {
            payload.insert("custom_fields".to_string(), Value::Object(object.clone()));
        }
        _ => {
            payload.insert("custom_fields".to_string(), Value::Object(Map::new()));
        }
    }
}

fn normalize_status(value: Option<&Value>) -> String {
    let raw = match value {
        Some(Value::String(text)) => text.as_str(),
        Some(other) => return normalize_status_text(&other.to_string()),
        None => "active",
    };
    normalize_status_text(raw)
}

fn normalize_status_text(raw: &str) -> String {
    match raw.trim().to_lowercase().as_str() {
        "running" | "online" | "active" => "active",
        "stopped" | "paused" | "offline" => "offline",
        "planned" => "planned",
        _ => "active",
    }
    .to_string()
}

fn value_to_i64(value: &Value) -> Option<i64> {
    match value {
        Value::Number(number) => number_to_i64(number),
        Value::String(text) => parse_i64(text),
        _ => None,
    }
}

fn number_to_i64(number: &Number) -> Option<i64> {
    number
        .as_i64()
        .or_else(|| number.as_u64().and_then(|value| i64::try_from(value).ok()))
        .or_else(|| number.as_f64().map(|value| value as i64))
}

fn parse_i64(text: &str) -> Option<i64> {
    text.trim().parse::<i64>().ok()
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    #[test]
    fn relation_id_accepts_int_string_and_nested_object() {
        assert_eq!(relation_id(Some(&json!(1))), Some(1));
        assert_eq!(relation_id(Some(&json!("2"))), Some(2));
        assert_eq!(relation_id(Some(&json!({"id": 3}))), Some(3));
        assert_eq!(relation_id(Some(&json!({"value": "4"}))), Some(4));
        assert_eq!(relation_id(Some(&json!({"id": "not-int"}))), None);
    }

    #[test]
    fn normalize_tags_sorts_and_deduplicates_ids() {
        assert_eq!(
            normalize_tags(Some(&json!([3, {"id": 1}, "2", 1]))),
            vec![1, 2, 3]
        );
    }
}

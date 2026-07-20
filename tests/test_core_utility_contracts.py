"""Behavioral coverage for shared validation and messaging utilities."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from proxbox_api.exception import SyncError
from proxbox_api.utils.error_handling import (
    early_return_if_empty,
    early_return_if_invalid_id,
    early_return_if_none,
    handle_sync_error,
    safe_getattr,
)
from proxbox_api.utils.netbox_helpers import (
    _relation_id,
    _relation_name,
    build_tag_refs,
    extract_ids,
    get_safe_attr,
    get_safe_id,
    normalize_record_to_dict,
)
from proxbox_api.utils.type_guards import (
    has_required_fields,
    is_netbox_record,
    is_proxmox_resource,
    is_tag_like,
    is_valid_id,
    is_valid_ip,
    is_valid_mac,
    is_valid_slug,
    safe_dict_get,
)
from proxbox_api.utils.websocket_utils import (
    send_error,
    send_phase_complete,
    send_phase_start,
    send_progress_update,
    send_status_message,
    send_summary,
)


class RecordingWebSocket:
    """Minimal WebSocket test double that records JSON payloads."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.payloads: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        if self.error is not None:
            raise self.error
        self.payloads.append(payload)


def test_sync_error_decorator_preserves_success_and_existing_sync_errors(monkeypatch):
    logger = SimpleNamespace(error=Mock())
    monkeypatch.setattr("proxbox_api.utils.error_handling.logger", logger)

    @handle_sync_error("read_inventory", resource_type="device", phase="query")
    def successful(value: int) -> int:
        return value + 1

    existing = SyncError("already contextualized", operation="existing")

    @handle_sync_error("read_inventory")
    def already_wrapped() -> None:
        raise existing

    assert successful(4) == 5
    with pytest.raises(SyncError) as exc_info:
        already_wrapped()

    assert exc_info.value is existing
    logger.error.assert_not_called()


def test_sync_error_decorator_wraps_unexpected_errors_with_context(monkeypatch):
    logger = SimpleNamespace(error=Mock())
    monkeypatch.setattr("proxbox_api.utils.error_handling.logger", logger)

    @handle_sync_error("sync_network", resource_type="interface", phase="write")
    def fail() -> None:
        raise RuntimeError("upstream unavailable")

    with pytest.raises(SyncError, match="Failed sync_network") as exc_info:
        fail()

    error = exc_info.value
    assert error.operation == "sync_network"
    assert error.resource_type == "interface"
    assert error.phase == "write"
    assert isinstance(error.original_error, RuntimeError)
    logger.error.assert_called_once()
    assert logger.error.call_args.kwargs["extra"] == {
        "operation": "sync_network",
        "resource_type": "interface",
        "phase": "write",
    }


def test_early_return_and_safe_attribute_helpers():
    early_return_if_none("present", "missing")
    early_return_if_empty([1], "empty")
    assert early_return_if_invalid_id(7, "invalid") == 7
    assert safe_getattr(SimpleNamespace(name="node-a"), "name") == "node-a"
    assert safe_getattr(object(), "name", "fallback") == "fallback"

    with pytest.raises(ValueError, match="missing"):
        early_return_if_none(None, "missing")
    with pytest.raises(ValueError, match="empty"):
        early_return_if_empty({}, "empty")
    for invalid_id in (None, 0, -1):
        with pytest.raises(ValueError, match="invalid"):
            early_return_if_invalid_id(invalid_id, "invalid")
    with pytest.raises(ValueError, match="Required attribute 'name' is None"):
        safe_getattr(SimpleNamespace(name=None), "name", raise_on_none=True)


def test_runtime_type_guards_cover_structural_and_scalar_validation():
    record = SimpleNamespace(id=1, name="node", slug="node", display="Node")
    tag = SimpleNamespace(name="managed", slug="managed", color="00ff00")

    assert is_netbox_record(record)
    assert not is_netbox_record(SimpleNamespace(id=1, name="node"))
    assert is_tag_like(tag)
    assert not is_tag_like(SimpleNamespace(name="managed", slug="managed"))
    assert is_proxmox_resource({"vmid": 101})
    assert not is_proxmox_resource(SimpleNamespace(get=lambda key: key))

    assert is_valid_id(1)
    assert not is_valid_id(0)
    assert not is_valid_id("not-an-id")
    assert not is_valid_id(None)
    assert is_valid_ip("192.0.2.10")
    assert is_valid_ip("2001:db8::10")
    assert not is_valid_ip("300.0.0.1")
    assert is_valid_mac("00:11:22:aa:BB:ff")
    assert is_valid_mac("00-11-22-aa-BB-ff")
    assert not is_valid_mac("00:11:22:33:44")
    assert is_valid_slug("node-101")
    assert not is_valid_slug("Node 101")


def test_dictionary_guards_accept_falsey_values_but_reject_missing_values():
    values = {"count": 0, "enabled": False, "name": "node"}
    getter = SimpleNamespace(get=lambda key, default=None: values.get(key, default))

    assert has_required_fields(values, "count", "enabled", "name")
    assert not has_required_fields({"name": None}, "name")
    assert not has_required_fields(values, "missing")
    assert safe_dict_get(values, "count", 99) == 0
    assert safe_dict_get(getter, "enabled", True) is False
    assert safe_dict_get(values, "missing", "fallback") == "fallback"
    assert safe_dict_get(None, "missing", "fallback") == "fallback"
    assert safe_dict_get(object(), "missing", "fallback") == "fallback"


def test_netbox_helpers_normalize_records_relations_tags_and_ids():
    tag = SimpleNamespace(name="Managed", slug="managed", color="00ff00")
    colorless = SimpleNamespace(name="Default", slug="default", color=None)
    incomplete = SimpleNamespace(name="Incomplete", slug=None, color="ffffff")
    record = SimpleNamespace(id="7", name="node-a", slug="node-a", status=None)

    assert get_safe_id(None) == 0
    assert get_safe_id(SimpleNamespace(id=0), default=None) is None
    assert get_safe_id(record) == 7
    assert get_safe_attr(None, "name", "fallback") == "fallback"
    assert get_safe_attr(record, "status", "offline") == "offline"
    assert build_tag_refs(None) == []
    assert build_tag_refs([tag, colorless, incomplete]) == [
        {"name": "Managed", "slug": "managed", "color": "00ff00"},
        {"name": "Default", "slug": "default", "color": "9e9e9e"},
    ]

    assert _relation_id(None) is None
    assert _relation_id({"id": "8"}) == {"id": 8}
    assert _relation_id({"id": None}) is None
    assert _relation_id(SimpleNamespace(id=9)) == {"id": 9}
    assert _relation_id("10") == {"id": 10}
    assert _relation_id("invalid") is None
    assert _relation_name(None) is None
    assert _relation_name({"name": "cluster-a"}) == {"name": "cluster-a"}
    assert _relation_name({"name": ""}) is None
    assert _relation_name(SimpleNamespace(name="cluster-b")) == {"name": "cluster-b"}
    assert _relation_name("cluster-c") == {"name": "cluster-c"}
    assert _relation_name(42) is None

    assert normalize_record_to_dict(None) == {}
    assert normalize_record_to_dict({"name": "node-a", "slug": None}) == {"name": "node-a"}
    assert normalize_record_to_dict(record, fields=["id", "name", "status"]) == {
        "id": "7",
        "name": "node-a",
    }
    assert extract_ids(None) == []
    assert extract_ids([record, SimpleNamespace(id=None), SimpleNamespace(id=11)]) == [7, 11]


@pytest.mark.asyncio
async def test_progress_and_status_messages_serialize_optional_fields():
    websocket = RecordingWebSocket()

    await send_progress_update(
        websocket,
        "syncing",
        progress=2,
        total=5,
        current_item="vm-101",
        level="DEBUG",
        use_css=True,
    )
    await send_status_message(websocket, "attention", status="warning")
    await send_status_message(websocket, "unknown", status="custom")
    await send_progress_update(None, "ignored")

    assert websocket.payloads == [
        {
            "message": "syncing",
            "level": "DEBUG",
            "progress": 2,
            "total": 5,
            "current_item": "vm-101",
            "use_css": True,
        },
        {"message": "attention", "level": "WARNING"},
        {"message": "unknown", "level": "INFO"},
    ]


@pytest.mark.asyncio
async def test_progress_send_failure_is_logged_and_suppressed(monkeypatch):
    logger = SimpleNamespace(warning=Mock())
    monkeypatch.setattr("proxbox_api.utils.websocket_utils.logger", logger)
    websocket = RecordingWebSocket(RuntimeError("connection closed"))

    await send_progress_update(websocket, "syncing")

    logger.warning.assert_called_once()
    assert "connection closed" in logger.warning.call_args.args[0]
    assert logger.warning.call_args.kwargs["extra"] == {"websocket_id": id(websocket)}


@pytest.mark.asyncio
async def test_phase_and_error_messages_preserve_context_and_outcome():
    websocket = RecordingWebSocket()

    await send_phase_start(websocket, "inventory", description="read endpoints", use_css=True)
    await send_phase_start(websocket, "network")
    await send_phase_complete(websocket, "inventory")
    await send_phase_complete(websocket, "network", success=False)
    await send_error(websocket, ValueError("invalid VM"), context="reconcile", use_css=True)
    await send_error(websocket, "plain failure")
    await send_error(None, "ignored")

    assert websocket.payloads == [
        {
            "message": "Starting phase: inventory - read endpoints",
            "level": "INFO",
            "use_css": True,
        },
        {"message": "Starting phase: network", "level": "INFO"},
        {"message": "Completed phase: inventory", "level": "INFO"},
        {"message": "Failed phase: network", "level": "ERROR"},
        {
            "message": "reconcile: ValueError: invalid VM",
            "level": "ERROR",
            "use_css": True,
        },
        {"message": "plain failure", "level": "ERROR"},
    ]


@pytest.mark.asyncio
async def test_summary_message_reports_changes_totals_and_failures():
    websocket = RecordingWebSocket()

    await send_summary(websocket, operation="inventory", total=0)
    await send_summary(
        websocket,
        created=1,
        updated=2,
        deleted=3,
        failed=4,
        skipped=5,
        total=15,
        operation="inventory",
        use_css=True,
    )
    await send_summary(None, created=1)

    assert websocket.payloads == [
        {"message": "Summary for inventory: No changes (total: 0)", "level": "INFO"},
        {
            "message": (
                "Summary for inventory: 1 created, 2 updated, 3 deleted, "
                "4 failed, 5 skipped (total: 15)"
            ),
            "level": "ERROR",
            "use_css": True,
        },
    ]

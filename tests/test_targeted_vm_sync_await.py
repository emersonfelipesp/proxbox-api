"""Regression tests for targeted single-VM sync NetBox record resolution.

netbox-proxbox issue #616: every targeted (single-VM) sync failed with

    Virtual machine id=<n> has no name and no proxmox_vm_id custom field
    to match in Proxmox.

even though a full-cluster sync of the same VM worked. The netbox-sdk accessor
``virtualization.virtual_machines.get()`` is ``async def``; the targeted sync
routes called it without ``await`` (``sync_vm.py``) or wrapped it in
``asyncio.to_thread`` (``backups_vm.py`` / ``disks_vm.py`` / ``snapshots_vm.py``),
both of which yield a *coroutine* instead of a ``Record``. ``to_mapping()`` then
silently degraded that coroutine to ``{}``, so the VM looked nameless and
custom-field-less and the 422 guard fired on every run.

These tests lock in both halves of the fix:

* the call sites must be awaited (so a real record is resolved), and
* ``to_mapping()`` must never silently swallow a non-coercible value again.
"""

from __future__ import annotations

import ast
import inspect
import logging
import pathlib

import pytest

from proxbox_api.services.sync.vm_helpers import to_mapping

# proxbox_api/services/sync/vm_helpers.py -> proxbox_api/
PROXBOX_API_PKG = pathlib.Path(inspect.getfile(to_mapping)).parents[2]
VM_ROUTES = PROXBOX_API_PKG / "routes" / "virtualization" / "virtual_machines"

TARGETED_SYNC_MODULES = (
    "sync_vm.py",
    "backups_vm.py",
    "disks_vm.py",
    "snapshots_vm.py",
)


@pytest.fixture
def proxbox_caplog(caplog):
    """Capture records from the app logger, which sets ``propagate = False``."""
    app_logger = logging.getLogger("proxbox")
    previous_level = app_logger.level
    app_logger.addHandler(caplog.handler)
    app_logger.setLevel(logging.DEBUG)
    try:
        yield caplog
    finally:
        app_logger.removeHandler(caplog.handler)
        app_logger.setLevel(previous_level)


class _Record:
    """Stand-in for a netbox-sdk ``Record`` (exposes ``serialize()``)."""

    def __init__(self, data: dict[str, object]) -> None:
        self._data = data

    def serialize(self) -> dict[str, object]:
        return self._data


class _AsyncEndpoint:
    """Stand-in for ``netbox_session.virtualization.virtual_machines``."""

    def __init__(self, record: _Record) -> None:
        self._record = record

    async def get(self, **_kwargs: object) -> _Record:
        return self._record


VM_PAYLOAD: dict[str, object] = {
    "id": 59,
    "name": "node57-k8s",
    "cluster": {"id": 4, "name": "pve-cluster-01"},
    "custom_fields": {"proxmox_vm_id": 254, "proxmox_endpoint_id": 1},
}


@pytest.mark.asyncio
async def test_awaited_vm_record_resolves_name_and_custom_fields():
    """The awaited record must expose name + custom_fields (post-fix behaviour)."""
    endpoint = _AsyncEndpoint(_Record(VM_PAYLOAD))

    vm_data = to_mapping(await endpoint.get(id=59))

    assert str(vm_data.get("name", "")).strip() == "node57-k8s"
    custom_fields = vm_data.get("custom_fields")
    assert isinstance(custom_fields, dict)
    assert custom_fields.get("proxmox_vm_id") == 254


def test_unawaited_coroutine_no_longer_degrades_silently(proxbox_caplog):
    """A missing ``await`` must be reported loudly instead of yielding a bare {}."""
    endpoint = _AsyncEndpoint(_Record(VM_PAYLOAD))
    coroutine = endpoint.get(id=59)
    try:
        vm_data = to_mapping(coroutine)
    finally:
        coroutine.close()

    # Still defensive (no crash), but now diagnosable.
    assert vm_data == {}
    assert any("un-awaited" in record.getMessage() for record in proxbox_caplog.records), (
        "to_mapping() must log an error when handed an un-awaited coroutine"
    )


@pytest.mark.parametrize("module_name", TARGETED_SYNC_MODULES)
def test_targeted_sync_routes_await_the_netbox_vm_lookup(module_name):
    """Every targeted-sync VM lookup must be directly awaited.

    Fails on the pre-fix code: ``sync_vm.py`` had a bare call and the other three
    wrapped the coroutine factory in ``asyncio.to_thread(lambda: ...)``.
    """
    source = (VM_ROUTES / module_name).read_text()
    tree = ast.parse(source)

    lookups: list[tuple[int, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "get":
            continue
        owner = func.value
        if not isinstance(owner, ast.Attribute) or owner.attr != "virtual_machines":
            continue
        parent_is_await = any(
            isinstance(ancestor, ast.Await) and ancestor.value is node
            for ancestor in ast.walk(tree)
        )
        lookups.append((node.lineno, parent_is_await))

    assert lookups, f"expected a virtual_machines.get() lookup in {module_name}"
    unawaited = [line for line, awaited in lookups if not awaited]
    assert not unawaited, (
        f"{module_name}: virtual_machines.get() is async and must be awaited directly; "
        f"un-awaited call(s) at line(s) {unawaited}"
    )


def test_to_mapping_supports_pydantic_v2_and_root_models():
    """``to_mapping`` must handle the shapes netbox-sdk / Pydantic can hand back."""

    class _V2Model:
        def model_dump(self) -> dict[str, object]:
            return {"name": "from-model-dump"}

    class _RootModel:
        root = {"name": "from-root"}

    assert to_mapping({"name": "plain"}) == {"name": "plain"}
    assert to_mapping(_V2Model()) == {"name": "from-model-dump"}
    assert to_mapping(_RootModel()) == {"name": "from-root"}
    assert to_mapping(None) == {}


def test_to_mapping_warns_on_uncoercible_value(proxbox_caplog):
    """An unknown shape must warn rather than pretend the record was empty."""
    assert to_mapping(object()) == {}

    assert any("could not coerce" in record.getMessage() for record in proxbox_caplog.records)

"""Regression tests for enum handling in the NetBox desired-state normalizers.

A Proxbox sync once wrote every node interface with ``type`` set to
``"netboxinterfacetype.bridge"``, which NetBox rejects with
``{"type": ["netboxinterfacetype.bridge is not a valid choice."]}``. The stage
therefore created zero ``dcim.Interface`` rows and failed the whole sync job.

The cause was that ``str()`` on a member of a ``class Foo(str, Enum)`` resolves
to ``Enum.__str__`` and returns the class-qualified name, not the member value.
Every normalizer in :mod:`proxbox_api.proxmox_to_netbox.models` that ends in
``str(...).strip().lower()`` therefore turned a correct enum member into an
invalid NetBox choice. Call sites were fixed to pass ``.value`` explicitly;
these tests pin the normalizers themselves so a call site that forgets to do so
degrades to a correct payload instead of a rejected one.
"""

from __future__ import annotations

from enum import Enum

import pytest

from proxbox_api.enum.status_mapping import NetBoxInterfaceType, ProxmoxToNetBoxVMStatus
from proxbox_api.proxmox_to_netbox.models import (
    NetBoxInterfaceSyncState,
    NetBoxVirtualMachineInterfaceSyncState,
)

# Transcribed once from NetBox 4.x ``OPTIONS /api/dcim/interfaces/``: the
# non-physical interface types a Proxmox node interface can legitimately map
# onto. Deliberately a fixed literal set rather than anything derived from
# NetBoxInterfaceType, so widening the enum cannot silently widen the oracle.
NETBOX_ACCEPTED_INTERFACE_TYPES = frozenset({"bridge", "lag", "virtual", "other"})


class TestNetBoxInterfaceTypeValues:
    def test_every_member_is_a_netbox_accepted_choice(self) -> None:
        """No member may carry a value NetBox would reject.

        ``loopback`` used to be a member. NetBox has no ``loopback`` interface
        type, so it was an invalid choice waiting for its mapping key to match.
        """
        offenders = {
            member.name: member.value
            for member in NetBoxInterfaceType
            if member.value not in NETBOX_ACCEPTED_INTERFACE_TYPES
        }
        assert offenders == {}

    @pytest.mark.parametrize(
        ("proxmox_type", "expected"),
        [
            ("bridge", "bridge"),
            ("bond", "lag"),
            ("vlan", "virtual"),
            # Everything else degrades to `other`, exactly as before. Widening
            # this table would retype existing NetBox rows and is a migration,
            # not a mapping fix -- see the note on the enum.
            ("lo", "other"),
            ("loopback", "other"),
            ("OVSBridge", "other"),
            ("OVSBond", "other"),
            ("eth", "other"),
            ("", "other"),
            (None, "other"),
        ],
    )
    def test_from_proxmox_maps_to_accepted_values(
        self, proxmox_type: object, expected: str
    ) -> None:
        resolved = NetBoxInterfaceType.from_proxmox(proxmox_type)
        assert resolved.value == expected
        assert resolved.value in NETBOX_ACCEPTED_INTERFACE_TYPES


class TestInterfaceSyncStateEnumNormalization:
    @pytest.mark.parametrize(
        ("proxmox_type", "expected"),
        [
            ("bridge", "bridge"),
            ("bond", "lag"),
            ("vlan", "virtual"),
            ("eth", "other"),
        ],
    )
    def test_enum_member_serializes_to_its_value(self, proxmox_type: str, expected: str) -> None:
        """Passing the member itself — not ``.value`` — must still be correct."""
        state = NetBoxInterfaceSyncState(
            device=1,
            name="vmbr0",
            type=NetBoxInterfaceType.from_proxmox(proxmox_type),
        )
        assert state.type == expected
        # A member compares equal to its value, so the type is the real check.
        assert type(state.type) is str

    def test_plain_string_is_unchanged(self) -> None:
        assert NetBoxInterfaceSyncState(device=1, name="eno1", type="bridge").type == "bridge"

    def test_netbox_choice_object_is_unwrapped(self) -> None:
        state = NetBoxInterfaceSyncState(
            device=1,
            name="eno1",
            type={"value": "bridge", "label": "Bridge"},
        )
        assert state.type == "bridge"

    def test_serialized_type_never_carries_the_class_name(self) -> None:
        """The exact shape NetBox rejected, asserted directly."""
        state = NetBoxInterfaceSyncState(
            device=1,
            name="vmbr0",
            type=NetBoxInterfaceType.bridge,
        )
        assert "netboxinterfacetype" not in state.type
        assert state.model_dump()["type"] == "bridge"


class TestVirtualMachineInterfaceSyncStateEnumNormalization:
    def test_type_enum_member_serializes_to_its_value(self) -> None:
        state = NetBoxVirtualMachineInterfaceSyncState(
            virtual_machine=1,
            name="net0",
            type=NetBoxInterfaceType.virtual,
        )
        assert state.type == "virtual"

    def test_mode_enum_member_serializes_to_its_value(self) -> None:
        class _Mode(str, Enum):
            access = "access"

        state = NetBoxVirtualMachineInterfaceSyncState(
            virtual_machine=1,
            name="net0",
            mode=_Mode.access,
        )
        assert state.mode == "access"


class TestStatusEnumNormalization:
    @pytest.mark.parametrize(
        ("proxmox_status", "expected"),
        [
            ("running", "active"),
            ("stopped", "offline"),
            ("paused", "offline"),
        ],
    )
    def test_status_member_serializes_to_its_value(
        self, proxmox_status: str, expected: str
    ) -> None:
        """``normalize_status`` shares the ``str(...)`` shape that broke ``type``.

        The exact type is the real assertion. A member of a ``(str, Enum)``
        class compares equal to its own value, so an equality check alone stays
        green with the unwrap removed -- and the consumer below then writes the
        class-qualified name to NetBox.
        """
        from proxbox_api.proxmox_to_netbox.models import _status_value

        member = ProxmoxToNetBoxVMStatus.from_proxmox(proxmox_status)
        unwrapped = _status_value(member)
        assert unwrapped == expected
        assert type(unwrapped) is str

    @pytest.mark.parametrize(
        ("proxmox_status", "expected"),
        [
            ("running", "active"),
            ("stopped", "offline"),
            ("paused", "offline"),
        ],
    )
    def test_a_model_consuming_the_helper_writes_the_wire_value(
        self, proxmox_status: str, expected: str
    ) -> None:
        """The downstream consumer, so the helper's contract is tested end to end.

        ``NetBoxDeviceSyncState.normalize_status`` runs
        ``str(_status_value(value)).lower()``. With the unwrap removed that
        produces the class-qualified name, which NetBox rejects as an invalid
        choice -- the same failure shape as the interface type defect.
        """
        from proxbox_api.proxmox_to_netbox.models import NetBoxDeviceSyncState

        member = ProxmoxToNetBoxVMStatus.from_proxmox(proxmox_status)
        state = NetBoxDeviceSyncState(name="node01", status=member)
        assert state.status == expected
        assert type(state.status) is str
        assert "proxmoxtonetboxvmstatus" not in state.status

    @pytest.mark.parametrize(
        ("proxmox_status", "expected"),
        [
            ("running", "active"),
            ("stopped", "offline"),
            ("paused", "offline"),
        ],
    )
    def test_mapping_a_mapped_member_again_is_a_no_op(
        self, proxmox_status: str, expected: str
    ) -> None:
        """Re-normalizing is idempotent.

        The status flows through two mappers in the live sync path, and the
        second used to receive the member the first produced. ``str()`` on that
        member matched no key, so it fell through to the ``active`` default and
        a stopped VM was recorded as running.
        """
        once = ProxmoxToNetBoxVMStatus.from_proxmox(proxmox_status)
        twice = ProxmoxToNetBoxVMStatus.from_proxmox(once)
        assert once.value == expected
        assert twice.value == expected

    def test_mapping_a_mapped_interface_type_again_is_a_no_op(self) -> None:
        member = NetBoxInterfaceType.from_proxmox("bridge")
        assert NetBoxInterfaceType.from_proxmox(member).value == "bridge"


class TestLiveVirtualMachineStatusPath:
    """Covers the payload builder and the model that consumes its output.

    Scope, stated honestly: this class exercises ``_build_netbox_vm_payload``
    and ``NetBoxVirtualMachineCreateBody``. It does **not** reach
    ``sync_vm_individual`` or reconciliation, and it is not by itself an oracle
    for the idempotence guard — with ``_status_value`` returning ``.value``, the
    builder never hands a member across the boundary, so removing only the
    guard leaves every test here green. That defence is covered separately by
    ``TestStatusEnumNormalization`` and by
    ``TestMemberCrossingTheModelBoundary`` below.

    There are two independent defences and each is tested on its own:

    1. The builder emits a plain wire string, so a member never crosses.
    2. If one crosses anyway, mapping it again is a no-op rather than a silent
       fall back to ``active``.

    Assertions check the exact type as well as the value, because a member of a
    ``(str, Enum)`` class compares equal to its own value — ``== "offline"``
    passes for both the string and the member, and is vacuous on its own.
    """

    @staticmethod
    def _payload(proxmox_status: str) -> dict:
        from datetime import datetime, timezone

        from proxbox_api.services.sync.individual.vm_sync import (
            _build_netbox_vm_payload,
        )

        return _build_netbox_vm_payload(
            resource={
                "vmid": 101,
                "name": "regression-vm",
                "status": proxmox_status,
                "node": "node01",
                "type": "qemu",
            },
            config={},
            cluster_id=1,
            device_id=None,
            role_id=None,
            tag_ids=[],
            last_updated=datetime.now(timezone.utc),
        )

    @pytest.mark.parametrize(
        ("proxmox_status", "expected"),
        [
            ("running", "active"),
            ("stopped", "offline"),
            ("paused", "offline"),
        ],
    )
    def test_built_payload_carries_the_wire_status(
        self, proxmox_status: str, expected: str
    ) -> None:
        status = self._payload(proxmox_status)["status"]
        assert status == expected
        # Defence 1: a plain string, not a member that happens to compare equal.
        assert type(status) is str

    @pytest.mark.parametrize(
        ("proxmox_status", "expected"),
        [
            ("running", "active"),
            ("stopped", "offline"),
            ("paused", "offline"),
        ],
    )
    def test_validated_body_serializes_the_wire_status(
        self, proxmox_status: str, expected: str
    ) -> None:
        from proxbox_api.proxmox_to_netbox.models import (
            NetBoxVirtualMachineCreateBody,
        )

        body = NetBoxVirtualMachineCreateBody.model_validate(self._payload(proxmox_status))
        status = body.model_dump()["status"]
        assert status == expected
        assert type(status) is str

    def test_compat_mapper_returns_a_string_not_a_member(self) -> None:
        """`map_status` is annotated `-> str`; it used to return the member."""
        from proxbox_api.netbox_compat import VirtualMachine

        mapped = VirtualMachine.map_status("stopped")
        assert mapped == "offline"
        assert type(mapped) is str


class TestMemberCrossingTheModelBoundary:
    """Defence 2, tested without relying on defence 1 holding.

    This is the shape the live defect actually had: a mapped member reaching the
    model's ``status`` validator, which mapped it a second time. Feeding the
    member in directly keeps this an oracle for the idempotence guard even
    though the builder no longer produces one.
    """

    @pytest.mark.parametrize(
        ("proxmox_status", "expected"),
        [
            ("running", "active"),
            ("stopped", "offline"),
            ("paused", "offline"),
        ],
    )
    def test_a_member_reaching_the_validator_keeps_its_value(
        self, proxmox_status: str, expected: str
    ) -> None:
        from proxbox_api.proxmox_to_netbox.models import (
            NetBoxVirtualMachineCreateBody,
        )

        member = ProxmoxToNetBoxVMStatus.from_proxmox(proxmox_status)
        assert isinstance(member, ProxmoxToNetBoxVMStatus)

        payload = TestLiveVirtualMachineStatusPath._payload(proxmox_status)
        payload["status"] = member  # the shape the defect travelled in
        body = NetBoxVirtualMachineCreateBody.model_validate(payload)

        status = body.model_dump()["status"]
        assert status == expected
        assert type(status) is str

    def test_a_member_reaching_the_interface_validator_keeps_its_value(self) -> None:
        state = NetBoxInterfaceSyncState(
            device=1,
            name="vmbr0",
            type=NetBoxInterfaceType.from_proxmox("bridge"),
        )
        assert state.type == "bridge"
        assert type(state.type) is str


class TestNormalizerHelpers:
    """Direct coverage of the shared helpers, independent of any one model."""

    def test_choice_value_unwraps_enum_members(self) -> None:
        from proxbox_api.proxmox_to_netbox.models import _choice_value

        assert _choice_value(NetBoxInterfaceType.lag) == "lag"

    def test_choice_value_still_unwraps_netbox_choice_dicts(self) -> None:
        from proxbox_api.proxmox_to_netbox.models import _choice_value

        assert _choice_value({"value": "lag", "label": "LAG"}) == "lag"
        assert _choice_value({"label": "LAG"}) == "LAG"

    def test_choice_value_passes_scalars_through(self) -> None:
        from proxbox_api.proxmox_to_netbox.models import _choice_value

        assert _choice_value("lag") == "lag"
        assert _choice_value(None) is None

    def test_content_type_value_unwraps_enum_members(self) -> None:
        from proxbox_api.proxmox_to_netbox.models import _content_type_value

        class _ObjectType(str, Enum):
            device = "dcim.device"

        assert _content_type_value(_ObjectType.device) == "dcim.device"

    def test_relation_id_unwraps_int_enum_members(self) -> None:
        from enum import IntEnum

        from proxbox_api.proxmox_to_netbox.models import _relation_id

        class _Id(IntEnum):
            first = 1

        assert _relation_id(_Id.first) == 1
        assert _relation_id({"id": 7}) == 7
        assert _relation_id(7) == 7

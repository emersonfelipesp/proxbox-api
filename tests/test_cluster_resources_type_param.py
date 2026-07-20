"""Regression tests for the ``cluster/resources`` ``type`` query parameter.

``ClusterResourcesType`` is a ``(str, Enum)``. Passing the enum member straight
through to proxmox-sdk urlencodes it as ``type=ClusterResourcesType.vm`` (because
``str(member)`` is ``"ClusterResourcesType.vm"``), which Proxmox rejects with
``HTTP 400 Parameter verification failed``. ``get_cluster_resources`` must send
the plain value (``"vm"``) instead. See issue #191.
"""

from proxbox_api.enum.proxmox import ClusterResourcesType
from proxbox_api.services.proxmox_helpers import get_cluster_resources


class _RecordingResource:
    def __init__(self, recorder: dict) -> None:
        self._recorder = recorder

    def get(self, **params: object) -> list:
        self._recorder["params"] = params
        # An empty resource list validates cleanly through the generated model.
        return []


class _RecordingSession:
    """Minimal stand-in for ``ProxmoxSession`` that records the ``.get`` kwargs."""

    def __init__(self) -> None:
        self.calls: dict = {}

    def session(self, path: str) -> _RecordingResource:
        self.calls["path"] = path
        return _RecordingResource(self.calls)


def test_enum_resource_type_is_sent_as_plain_string() -> None:
    session = _RecordingSession()

    get_cluster_resources(session, ClusterResourcesType.vm)

    assert session.calls["path"] == "cluster/resources"
    sent = session.calls["params"]["type"]
    # The bug sent the enum member, which stringifies to "ClusterResourcesType.vm"
    # and makes Proxmox return HTTP 400. It must be the plain value instead.
    assert sent == "vm"
    assert str(sent) == "vm"


def test_plain_string_resource_type_passthrough() -> None:
    session = _RecordingSession()

    get_cluster_resources(session, "node")

    assert session.calls["params"]["type"] == "node"


def test_no_resource_type_omits_type_param() -> None:
    session = _RecordingSession()

    get_cluster_resources(session)

    assert "type" not in session.calls["params"]

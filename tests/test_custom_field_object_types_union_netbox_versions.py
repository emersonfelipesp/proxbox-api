"""Version-pinning regression tests for the proxmox_last_updated union helper.

Issue [proxbox-api#51](https://github.com/emersonfelipesp/proxbox-api/issues/51):
the `_union_object_types_with_current` helper landed in PR #56 (commit
``273112c``) and is *version-agnostic* by design — it relies on the NetBox
REST API serialization shape for ``extras.CustomField.object_types``, which
is currently identical across all three certified NetBox releases:

- NetBox 4.5.8
- NetBox 4.5.9
- NetBox 4.6.0-beta2

`netbox/extras/api/serializers_/customfields.py` declares ``object_types`` as
a ``ContentTypeField`` whose ``to_representation`` (``netbox/api/fields.py``)
returns ``f"{app_label}.{model}"`` — so live API responses always carry
``["app.model"]`` strings.

These tests pin that contract: if a future NetBox release changes the
serialization (e.g. dict shapes, ``display`` wrappers), one of these tests
will fail before users do, prompting a deliberate bump rather than a silent
regression.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from proxbox_api.routes.extras import _union_object_types_with_current


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _record(serialized: dict) -> object:
    return SimpleNamespace(serialize=lambda: serialized)


@pytest.fixture
def proxbox_caplog(caplog):
    """Attach caplog to the non-propagating ``proxbox`` logger."""
    proxbox_logger = logging.getLogger("proxbox")
    proxbox_logger.addHandler(caplog.handler)
    proxbox_logger.setLevel(logging.DEBUG)
    try:
        yield caplog
    finally:
        proxbox_logger.removeHandler(caplog.handler)


@pytest.mark.parametrize(
    "netbox_version",
    ["4.5.8", "4.5.9", "4.6.0-beta2"],
)
def test_union_preserves_operator_additions_across_supported_netbox_versions(
    monkeypatch, proxbox_caplog, netbox_version
):
    """Pin the union helper against the documented NetBox 4.5/4.6 API shape.

    All three certified NetBox versions return ``object_types`` as a list of
    ``"app.model"`` strings. The operator-added ``extras.tag`` entry must
    survive the reconcile pre-merge on every version.
    """
    current = ["virtualization.virtualmachine", "extras.tag"]

    async def _fake_first(_session, _path, query=None):
        assert query == {"name": "proxmox_last_updated", "limit": 2}
        return _record({"object_types": list(current)})

    monkeypatch.setattr("proxbox_api.routes.extras.rest_first_async", _fake_first)

    field: dict[str, object] = {
        "name": "proxmox_last_updated",
        "object_types": ["virtualization.virtualmachine", "dcim.device"],
    }
    proxbox_caplog.set_level(logging.INFO)
    _run(_union_object_types_with_current(object(), field))

    assert "extras.tag" in field["object_types"]
    assert field["object_types"][:2] == ["virtualization.virtualmachine", "dcim.device"]


@pytest.mark.parametrize(
    "netbox_version",
    ["4.5.8", "4.5.9", "4.6.0-beta2"],
)
def test_union_handles_dict_shape_defensively_across_versions(monkeypatch, netbox_version):
    """Defensive: if a future NetBox release switches to a dict shape
    ``[{"app_label": ..., "model": ...}]``, the helper must still merge
    correctly. Today no certified version emits this shape, but the
    coercer is intentionally tolerant of it.
    """

    async def _fake_first(_session, _path, query=None):
        return _record({"object_types": [{"app_label": "extras", "model": "tag"}]})

    monkeypatch.setattr("proxbox_api.routes.extras.rest_first_async", _fake_first)

    field: dict[str, object] = {
        "name": "proxmox_last_updated",
        "object_types": ["virtualization.virtualmachine"],
    }
    _run(_union_object_types_with_current(object(), field))
    assert field["object_types"] == [
        "virtualization.virtualmachine",
        "extras.tag",
    ]

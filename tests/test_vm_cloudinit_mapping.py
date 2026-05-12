"""Cloud-init reflection mapping (issue #363): decode, truncate, payload, gate."""

from __future__ import annotations

import urllib.parse

import pytest

from proxbox_api.proxmox_to_netbox.models import (
    CLOUDINIT_SSHKEYS_MAX_BYTES,
    CLOUDINIT_SSHKEYS_TRUNCATION_SENTINEL,
    NetBoxCloudInitSyncState,
    ProxmoxVmConfigInput,
)

SAMPLE_KEY = "ssh-rsa AAAAB3NzaC1yc2E_TEST_KEY ubuntu@host"


def test_sshkeys_are_url_decoded() -> None:
    """``%0A`` newlines must decode before the row is written."""
    raw = urllib.parse.quote(f"{SAMPLE_KEY}\nssh-ed25519 AAA other@host\n", safe="")
    config = ProxmoxVmConfigInput.model_validate({"sshkeys": raw})
    assert config.sshkeys is not None
    assert "%0A" not in config.sshkeys
    assert "\n" in config.sshkeys
    assert SAMPLE_KEY in config.sshkeys
    assert not config.sshkeys_truncated


def test_sshkeys_truncated_above_10_kb() -> None:
    long_key = SAMPLE_KEY + " " + ("x" * (CLOUDINIT_SSHKEYS_MAX_BYTES + 1024))
    raw = urllib.parse.quote(long_key, safe="")
    config = ProxmoxVmConfigInput.model_validate({"sshkeys": raw})
    assert config.sshkeys is not None
    assert config.sshkeys.endswith(CLOUDINIT_SSHKEYS_TRUNCATION_SENTINEL)
    assert config.sshkeys_truncated is True
    body = config.sshkeys[: -len(CLOUDINIT_SSHKEYS_TRUNCATION_SENTINEL)]
    assert len(body.encode("utf-8")) <= CLOUDINIT_SSHKEYS_MAX_BYTES


def test_cloudinit_payload_returns_none_when_no_keys_set() -> None:
    config = ProxmoxVmConfigInput.model_validate({"agent": "1"})
    assert config.cloudinit_payload() is None


def test_cloudinit_payload_carries_all_three_fields() -> None:
    config = ProxmoxVmConfigInput.model_validate(
        {
            "ciuser": "ubuntu",
            "sshkeys": urllib.parse.quote(SAMPLE_KEY + "\n", safe=""),
            "ipconfig0": "ip=dhcp",
        }
    )
    payload = config.cloudinit_payload()
    assert payload is not None
    assert payload["ciuser"] == "ubuntu"
    assert payload["ipconfig0"] == "ip=dhcp"
    assert SAMPLE_KEY in payload["sshkeys"]
    assert payload["sshkeys_truncated"] is False


def test_netbox_cloudinit_sync_state_normalizes_vm_relation() -> None:
    state = NetBoxCloudInitSyncState.model_validate(
        {
            "virtual_machine": {"id": 42},
            "ciuser": "ubuntu",
            "sshkeys": "ssh-rsa AAAA\n",
            "ipconfig0": "ip=dhcp",
        }
    )
    assert state.virtual_machine == 42


def test_netbox_cloudinit_sync_state_coerces_none_to_empty_string() -> None:
    state = NetBoxCloudInitSyncState.model_validate(
        {
            "virtual_machine": 7,
            "ciuser": None,
            "sshkeys": None,
            "ipconfig0": None,
        }
    )
    assert state.ciuser == ""
    assert state.sshkeys == ""
    assert state.ipconfig0 == ""


def test_vmconfig_accepts_explicit_cloudinit_keys() -> None:
    """The strict VMConfig validator must no longer raise on cloud-init keys."""
    from proxbox_api.schemas.virtualization import VMConfig

    config = VMConfig.model_validate(
        {
            "ciuser": "ubuntu",
            "sshkeys": urllib.parse.quote("ssh-rsa AAAA\n", safe=""),
            "ipconfig0": "ip=dhcp",
        }
    )
    assert config.ciuser == "ubuntu"
    assert config.ipconfig0 == "ip=dhcp"
    # VMConfig does not decode sshkeys (it stays raw on this schema); only
    # ProxmoxVmConfigInput runs the unquote validator.
    assert "%0A" in (config.sshkeys or "")


@pytest.mark.parametrize("flag", [True, False])
def test_cloudinit_payload_has_truncation_marker_in_sync_state(flag: bool) -> None:
    payload = {
        "virtual_machine": 1,
        "ciuser": "ubuntu",
        "sshkeys": "ssh-rsa AAAA\n",
        "ipconfig0": "ip=dhcp",
        "sshkeys_truncated": flag,
    }
    state = NetBoxCloudInitSyncState.model_validate(payload)
    assert state.sshkeys_truncated is flag

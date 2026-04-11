"""Tests for proxmox-sdk dependency wiring."""

from __future__ import annotations


def test_proxmox_sdk_mock_entrypoint_is_importable() -> None:
    import proxmox_sdk.mock_main

    assert proxmox_sdk.mock_main.app is not None
